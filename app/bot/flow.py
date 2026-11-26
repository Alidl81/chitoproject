from __future__ import annotations

import logging
import mimetypes
import re
from datetime import UTC
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.files import MAX_DOCUMENT_CAPTION, StoredUpload, UploadRejected, register_download
from app.bot.review import dismiss_previews, record_decision, support_context
from app.bot.transport import BotAdapter, ProviderError
from app.config import settings
from app.models import (
    AdminUser,
    Attachment,
    BotLinkChallenge,
    Conversation,
    ConversationFile,
    ConversationMessage,
    Customer,
    OutboxMessage,
    PlatformIdentity,
    Project,
    ProjectEvent,
)
from app.permissions import Role, has_permission
from app.security import digest_token, now_utc
from app.stats import bot_stats

logger = logging.getLogger("chitoproject.bot")
MAX_TEXT_CHARS = 5000
MAX_FILES_PER_PROJECT = 5
MAX_TOTAL_UPLOAD_BYTES = settings.max_upload_mb * 1024 * 1024
STATUS_LABELS = {
    "submitted": "ثبت‌شده",
    "reviewing": "در حال بررسی",
    "needs_info": "نیازمند اطلاعات",
    "in_progress": "در حال انجام",
    "completed": "تکمیل‌شده",
    "declined": "پذیرفته‌نشده",
    "canceled": "لغوشده",
}


def _label(user: dict[str, Any]) -> str:
    parts = [str(user.get("first_name", "")).strip(), str(user.get("last_name", "")).strip()]
    name = " ".join(part for part in parts if part)
    return name[:160] or (f"@{user['username']}" if user.get("username") else "کاربر")


def _clean_digits(value: str) -> str:
    return value.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def _parse_budget(value: str) -> int | None:
    normalized = _clean_digits(value).replace(",", "").replace("٬", "").replace(" ", "")
    if not normalized:
        return None
    if not re.fullmatch(r"\d{1,13}", normalized):
        raise ValueError("بودجه را فقط به‌صورت عدد تومان وارد کنید.")
    amount = int(normalized)
    if amount < 0 or amount > 10**12:
        raise ValueError("مبلغ واردشده خارج از بازهٔ مجاز است.")
    return amount


def _button(
    text: str,
    callback: str | None = None,
    url: str | None = None,
    *,
    style: str | None = None,
) -> dict[str, str]:
    value = {"text": text}
    if callback:
        if len(callback.encode("utf-8")) > 64:
            raise ValueError("callback data exceeds provider limit")
        value["callback_data"] = callback
    if url:
        value["url"] = url
    if style is not None:
        if style not in {"primary", "success", "danger"}:
            raise ValueError("unsupported inline button style")
        value["style"] = style
    return value


def _home_keyboard(staff: bool = False, can_view_all: bool = False, can_report: bool = False) -> list[list[dict[str, str]]]:
    if staff:
        if can_view_all:
            rows = [[_button("پروژه‌های من", "a:projects"), _button("صف بررسی", "a:queue")]]
        else:
            rows = [[_button("پروژه‌های من", "a:projects")]]
        if can_report:
            rows.append([_button("گزارش آماری", "a:stats:1", style="primary")])
        rows.append([_button("ورود به پنل مدیریت", url=f"{settings.public_base_url.rstrip('/')}/admin")])
        return rows
    return [
        [
            _button("ثبت پروژه", "c:new", style="primary"),
            _button("پیگیری", "c:projects"),
        ],
        [_button("پشتیبانی", "c:help")],
    ]


def _support_session_id(conversation: Conversation) -> str | None:
    value = (conversation.draft_data or {}).get("support_session_id")
    return str(value) if value else None


def _support_keyboard() -> list[list[dict[str, str]]]:
    return [[_button("پاک کردن صفحه", "c:clear", style="danger"),
             _button("بازگشت به منو", "c:home")]]


def _is_start(text: str) -> tuple[bool, str]:
    value = text.strip().split(maxsplit=1)
    if not value or not value[0].split("@", 1)[0].casefold() == "/start":
        return False, ""
    return True, value[1].strip() if len(value) > 1 else ""


def _update_parts(update: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") if isinstance(callback.get("message"), dict) else None
        return message, callback
    message = update.get("message") or update.get("edited_message")
    return (message if isinstance(message, dict) else None), None


async def _identity(db: AsyncSession, platform: str, user: dict[str, Any], chat_id: str,
                    *, create_customer: bool) -> PlatformIdentity:
    provider_id = str(user.get("id", ""))
    identity = await db.scalar(select(PlatformIdentity).where(
        PlatformIdentity.platform == platform,
        PlatformIdentity.provider_user_id == provider_id,
    ).with_for_update())
    if identity is None:
        identity = PlatformIdentity(platform=platform, provider_user_id=provider_id,
                                    chat_id=chat_id, username=user.get("username"))
        db.add(identity)
        await db.flush()
    identity.chat_id = chat_id
    identity.username = user.get("username")
    identity.last_seen_at = now_utc()
    if create_customer and identity.customer_id is None:
        customer = Customer(display_name=_label(user))
        db.add(customer)
        await db.flush()
        identity.customer_id = customer.id
    elif identity.customer_id:
        customer = await db.get(Customer, identity.customer_id)
        if customer and not customer.display_name:
            customer.display_name = _label(user)
    return identity


async def _conversation(db: AsyncSession, identity: PlatformIdentity, platform: str, chat_id: str) -> Conversation:
    row = await db.scalar(select(Conversation).where(Conversation.platform == platform,
                                                     Conversation.chat_id == chat_id).with_for_update())
    if row is None:
        row = Conversation(platform=platform, chat_id=chat_id,
                           customer_id=identity.customer_id, admin_id=identity.admin_id,
                           state="home", draft_data={})
        db.add(row)
        await db.flush()
    else:
        row.customer_id = identity.customer_id
        row.admin_id = identity.admin_id
        row.updated_at = now_utc()
    return row


async def _archive_inbound(db: AsyncSession, conversation: Conversation, update: dict[str, Any], message: dict[str, Any],
                           user: dict[str, Any], *, edited: bool = False) -> ConversationMessage:
    text = str(message.get("text") or message.get("caption") or "").strip()
    if text.startswith("/start link_"):
        text = "/start [لینک اتصال تیم]"
    file_info = _file_from_message(message)
    message_kind = file_info[4] if file_info else _message_kind(message)
    metadata: dict[str, Any] = {}
    if file_info:
        metadata = {
            "file_name": file_info[1],
            "file_size": file_info[3],
            "mime_type": file_info[2],
        }
        if not text:
            text = f"[پیوست: {metadata['file_name']}]"
    elif message_kind != "text":
        text = text or f"[پیام {message_kind} دریافت شد]"
        metadata = _message_metadata(message, message_kind)
    support_session_id = _support_session_id(conversation)
    if conversation.state == "support" and support_session_id:
        metadata["support_session_id"] = support_session_id
    provider_id = str(message.get("message_id", "")) or None
    revision = 1
    if edited and provider_id:
        previous = await db.scalar(select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.provider_message_id == provider_id,
        ).order_by(ConversationMessage.revision.desc()).limit(1).with_for_update())
        if previous:
            revision = previous.revision + 1
            for key in ("support_session_id", "support_decision", "support_reason"):
                if key in (previous.metadata_json or {}):
                    metadata[key] = previous.metadata_json[key]
    row = ConversationMessage(
        conversation_id=conversation.id, provider_message_id=provider_id, revision=revision,
        direction="inbound", actor_label=_label(user), content=text[:MAX_TEXT_CHARS],
        message_kind=message_kind, metadata_json=metadata,
    )
    db.add(row)
    conversation.updated_at = now_utc()
    return row


async def _archive_event(db: AsyncSession, conversation: Conversation, user_label: str, content: str,
                         update: dict[str, Any]) -> None:
    metadata = {"update_id": str(update.get("update_id", ""))}
    callback_data = (update.get("callback_query") or {}).get("data")
    if callback_data:
        metadata["callback"] = str(callback_data)[:64]
    if session_id := _support_session_id(conversation):
        metadata["support_session_id"] = session_id
    db.add(ConversationMessage(
        conversation_id=conversation.id, direction="event", actor_label=user_label,
        content=content[:500], message_kind="interaction",
        metadata_json=metadata,
    ))
    conversation.updated_at = now_utc()


async def _queue_ui(db: AsyncSession, conversation: Conversation, text: str,
                    keyboard: list[list[dict[str, str]]] | None, update: dict[str, Any],
                    *, force_send: bool = False) -> None:
    body = text[:3900]
    operation = "send" if force_send or not conversation.ui_message_id else "edit"
    provider_message_id = None if operation == "send" else conversation.ui_message_id
    revision = 1
    if provider_message_id:
        revision = int(await db.scalar(select(func.max(ConversationMessage.revision)).where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.provider_message_id == provider_message_id,
        )) or 0) + 1
    metadata = {"delivery_status": "queued"}
    if session_id := _support_session_id(conversation):
        metadata["support_session_id"] = session_id
    message = ConversationMessage(
        conversation_id=conversation.id, provider_message_id=provider_message_id, revision=revision,
        direction="outbound", actor_label="پیام راهنمای تیم", content=body,
        message_kind="text", metadata_json=metadata,
    )
    db.add(message)
    await db.flush()
    update_id = str(update.get("update_id", "unknown"))
    dedupe = f"ui:{conversation.platform}:{update_id}:{revision}"
    db.add(OutboxMessage(
        dedupe_key=dedupe[:160], platform=conversation.platform, chat_id=conversation.chat_id,
        conversation_id=conversation.id, transcript_message_id=message.id,
        operation=operation, provider_message_id=provider_message_id, body=body,
        keyboard=keyboard, use_as_ui=True, force_send=force_send,
    ))
    if operation == "send":
        conversation.ui_message_id = None
    conversation.updated_at = now_utc()


async def _queue_delete(db: AsyncSession, conversation: Conversation, message: ConversationMessage | None,
                        update: dict[str, Any], *, provider_message_id: str | None = None) -> None:
    provider_message_id = provider_message_id or (message.provider_message_id if message else None)
    if not provider_message_id:
        return
    update_id = str(update.get("update_id", "unknown"))
    digest_input = f"delete:{conversation.platform}:{conversation.id}:{provider_message_id}:{update_id}"
    dedupe = f"delete:{digest_token(digest_input)}"
    db.add(OutboxMessage(
        dedupe_key=dedupe, platform=conversation.platform, chat_id=conversation.chat_id,
        conversation_id=conversation.id, transcript_message_id=message.id if message else None,
        operation="delete", provider_message_id=provider_message_id,
        body="", keyboard=None, use_as_ui=False,
    ))


async def _queue_staff_notice(db: AsyncSession, identity: PlatformIdentity, admin: AdminUser,
                              project: Project) -> None:
    conversation = await _conversation(db, identity, identity.platform, identity.chat_id)
    budget = f"{project.budget_toman:,} تومان" if project.budget_toman is not None else "اعلام نشده"
    body = (f"درخواست تازه · {project.public_id}\n{project.title}\n\n{project.description[:1000]}"
            f"\n\nزمان: {project.deadline_note or 'اعلام نشده'} · بودجه: {budget}")
    await _send_review_preview(db, conversation, body, "p", project.id,
                               f"new-project:{project.id}:{admin.id}:{identity.platform}")


async def _send_review_preview(db: AsyncSession, conversation: Conversation, body: str,
                               kind: str, object_id: str, key: str) -> None:
    message = ConversationMessage(conversation_id=conversation.id, direction="outbound",
                                  actor_label="اعلان تیم", content=body,
                                  metadata_json={"delivery_status": "queued"})
    db.add(message)
    await db.flush()
    db.add(OutboxMessage(
        dedupe_key=key, platform=conversation.platform, chat_id=conversation.chat_id,
        conversation_id=conversation.id, transcript_message_id=message.id,
        operation="send", body=body, keyboard=[[
            _button("تأیید و بررسی", f"a:{kind}a:{object_id}", style="success"),
            _button("رد", f"a:{kind}r:{object_id}", style="danger"),
        ]],
    ))


async def _notify_support_team(db: AsyncSession, conversation: Conversation,
                               inbound: ConversationMessage) -> None:
    recipients = (await db.execute(
        select(PlatformIdentity, AdminUser)
        .join(AdminUser, AdminUser.id == PlatformIdentity.admin_id)
        .where(AdminUser.active.is_(True))
        .order_by(PlatformIdentity.platform, PlatformIdentity.id)
    )).all()
    body = f"درخواست پشتیبانی · {inbound.actor_label or 'مشتری'}\n\n{inbound.content[:1000]}"
    for identity, admin in recipients:
        if not has_permission(admin.role, "customers.read") or not has_permission(admin.role, "messages.reply"):
            continue
        chat = await _conversation(db, identity, identity.platform, identity.chat_id)
        await _send_review_preview(db, chat, body, "s", inbound.id,
                                   f"support-review:{inbound.id}:{admin.id}:{identity.platform}")


async def _decide_preview(db: AsyncSession, staff: AdminUser, kind: str, object_id: str,
                          accepted: bool, reason: str) -> bool:
    project = None
    source = None
    if kind == "p" and has_permission(staff.role, "projects.review"):
        project = await db.get(Project, object_id, with_for_update=True)
        if not project or project.status != "submitted":
            return False
        conversation_id = project.conversation_id
        key = f"new-project:{project.id}"
    elif kind == "s" and has_permission(staff.role, "customers.read") and has_permission(staff.role, "messages.reply"):
        source = await db.get(ConversationMessage, object_id, with_for_update=True)
        if not source or not support_context(source) or (source.metadata_json or {}).get("support_decision"):
            return False
        conversation_id = source.conversation_id
        key = f"support-review:{source.id}"
    else:
        return False
    customer = await db.get(Conversation, conversation_id)
    if not customer or not customer.customer_id or customer.admin_id is not None:
        return False
    if project is not None:
        project.status = "reviewing" if accepted else "declined"
        project.updated_at = now_utc()
        db.add(ProjectEvent(project_id=project.id, actor_type="admin", actor_id=str(staff.id),
                            event_type="status", note=reason, private=False))
        text = f"درخواست {project.public_id} {'در حال بررسی است.' if accepted else 'پذیرفته نشد.'}"
    else:
        versions = [source]
        if source.provider_message_id:
            versions = list(await db.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == source.conversation_id,
                ConversationMessage.provider_message_id == source.provider_message_id,
                ConversationMessage.direction == "inbound",
            ).order_by(ConversationMessage.revision).with_for_update()))
        for version in versions:
            version.metadata_json = {**(version.metadata_json or {}),
                                     "support_decision": "reviewing" if accepted else "declined",
                                     "support_reason": reason}
        text = "پیام پشتیبانی‌ات در حال بررسی است." if accepted else "درخواست پشتیبانی‌ات پذیرفته نشد."
    if reason:
        text += f"\nدلیل: {reason}"
    await queue_customer_message(db, customer, staff, text, f"review-result:{kind}:{object_id}")
    await dismiss_previews(db, key)
    record_decision(db, staff.id, "project" if project else "support", object_id, accepted, reason)
    return True


def _callback_description(data: str) -> str:
    descriptions = {
        "c:new": "ثبت پروژه",
        "c:track:computer": "انتخاب رشتهٔ کامپیوتر",
        "c:projects": "پیگیری درخواست‌ها",
        "c:help": "پشتیبانی",
        "c:clear": "پاک‌سازی صفحهٔ گفتگو",
        "c:clear_yes": "تأیید پاک‌سازی صفحهٔ گفتگو",
        "c:clear_cancel": "انصراف از پاک‌سازی صفحهٔ گفتگو",
        "c:confirm": "تأیید و ثبت درخواست",
        "c:cancel": "لغو فرایند جاری",
        "a:projects": "مشاهدهٔ پروژه‌های واگذارشده",
        "a:queue": "مشاهدهٔ صف بررسی",
    }
    return descriptions.get(data, "مشاهده یا ویرایش درخواست")


async def _show_home(db: AsyncSession, conversation: Conversation, update: dict[str, Any], *, force_send: bool = False,
                     staff: bool = False, can_view_all: bool = False, notice: str = "",
                     greeting: str = "", can_report: bool = False) -> None:
    conversation.state = "home"
    conversation.draft_data = {}
    if notice:
        text = notice
    elif staff:
        text = "به فضای کاری تیم خوش آمدید. درخواست‌های واگذارشده و صف بررسی از اینجا در دسترس است."
    else:
        text = f"سلام {greeting} 👋 از کجا شروع کنیم؟" if greeting else "سلام 👋 از کجا شروع کنیم؟"
    await _queue_ui(db, conversation, text, _home_keyboard(staff, can_view_all, can_report), update, force_send=force_send)


async def _finish_admin_link(db: AsyncSession, identity: PlatformIdentity, platform: str, token: str,
                             conversation: Conversation, update: dict[str, Any], user: dict[str, Any]) -> bool:
    challenge = await db.scalar(select(BotLinkChallenge).where(
        BotLinkChallenge.token_digest == digest_token(token), BotLinkChallenge.platform == platform,
    ).with_for_update())
    challenge_expiry = challenge.expires_at if challenge and challenge.expires_at.tzinfo else (
        challenge.expires_at.replace(tzinfo=UTC) if challenge else None
    )
    if not challenge or challenge.used_at or not challenge_expiry or challenge_expiry <= now_utc():
        return False
    admin = await db.get(AdminUser, challenge.admin_id)
    if not admin or not admin.active:
        return False
    if identity.admin_id not in (None, admin.id):
        return False
    other_identity = await db.scalar(select(PlatformIdentity.id).where(
        PlatformIdentity.platform == platform, PlatformIdentity.admin_id == admin.id,
        PlatformIdentity.id != identity.id,
    ).limit(1))
    if other_identity:
        return False
    identity.admin_id = admin.id
    challenge.used_at = now_utc()
    conversation.admin_id = admin.id
    await _archive_event(db, conversation, _label(user), "حساب همکار به پیام‌رسان متصل شد.", update)
    return True


async def _staff_menu(db: AsyncSession, conversation: Conversation, update: dict[str, Any], admin: AdminUser,
                      *, force_send: bool = False, notice: str = "") -> None:
    await _show_home(db, conversation, update, force_send=force_send, staff=True,
                     can_view_all=has_permission(admin.role, "projects.read"),
                     can_report=has_permission(admin.role, "reports.read"), notice=notice)


async def _project_list(db: AsyncSession, conversation: Conversation, update: dict[str, Any], customer_id: str,
                        *, staff: AdminUser | None = None, all_projects: bool = False) -> None:
    query = select(Project)
    if staff is None:
        query = query.where(Project.customer_id == customer_id)
    elif staff.role == Role.CONTRIBUTOR.value:
        query = query.where(Project.assigned_admin_id == staff.id)
    elif not all_projects:
        query = query.where(Project.assigned_admin_id == staff.id)
    rows = list((await db.scalars(query.order_by(Project.updated_at.desc()).limit(12))).all())
    if not rows:
        text = "درخواستی برای پیگیری پیدا نشد." if staff is None else "پروژه‌ای برای نمایش در دسترس نیست."
        await _queue_ui(db, conversation, text, [[_button("بازگشت به منو", "c:home" if staff is None else "a:home")]], update)
        return
    keyboard = [[_button(f"{p.public_id} · {p.title[:22]}",
                         f"{'a' if staff else 'c'}:project:{p.public_id}")] for p in rows]
    keyboard.append([_button("بازگشت به منو", "a:home" if staff else "c:home")])
    text = "یکی از درخواست‌ها را انتخاب کن:" if staff is None else "پروژه‌ای را برای مشاهدهٔ وضعیت انتخاب کنید:"
    await _queue_ui(db, conversation, text, keyboard, update)


def _staff_project_scope(query, staff: AdminUser):
    if not has_permission(staff.role, "projects.read"):
        return query.where(Project.assigned_admin_id == staff.id)
    return query


async def _project_view(db: AsyncSession, conversation: Conversation, update: dict[str, Any], public_id: str,
                        customer_id: str | None = None, staff: AdminUser | None = None) -> None:
    if staff:
        conversation.state = "home"
        conversation.draft_data = {}
    query = select(Project).where(Project.public_id == public_id)
    if customer_id:
        query = query.where(Project.customer_id == customer_id)
    if staff:
        query = _staff_project_scope(query, staff)
    project = await db.scalar(query)
    if not project:
        await _queue_ui(db, conversation, "این درخواست در دسترس نیست.",
                        [[_button("بازگشت", "a:projects" if staff else "c:projects")]], update)
        return
    text = (
        f"درخواست {project.public_id}\n{project.title}\n\n"
        f"وضعیت: {STATUS_LABELS.get(project.status, project.status)}\n"
        f"زمان موردنظر: {project.deadline_note or 'اعلام نشده'}\n"
        f"بودجه: {f'{project.budget_toman:,} تومان' if project.budget_toman is not None else 'اعلام نشده'}"
    )
    keyboard = [[_button("بازگشت به فهرست", "a:projects" if staff else "c:projects")],
                [_button("منوی اصلی", "a:home" if staff else "c:home")]]
    if staff and has_permission(staff.role, "messages.reply"):
        keyboard.insert(0, [_button("ارسال فایل به مشتری", f"a:file:{project.public_id}", style="primary")])
    await _queue_ui(db, conversation, text, keyboard, update)


async def _support_history(db: AsyncSession, conversation: Conversation) -> list[ConversationMessage]:
    session_field = ConversationMessage.metadata_json["support_session_id"].as_string()
    rows = list((await db.scalars(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.direction.in_(("inbound", "outbound")),
            session_field.is_not(None),
        )
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(40)
    )).all())
    if rows:
        visible = [row for row in rows if row.actor_label != "پیام راهنمای تیم"]
        return list(reversed(visible[:8]))

    # Show archived support exchanges created by older releases, before session tags existed.
    marker = await db.scalar(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.direction == "event",
            ConversationMessage.content.in_(("راهنما و ارتباط با تیم", "پشتیبانی")),
        )
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(1)
    )
    if not marker:
        return []
    legacy_rows = list((await db.scalars(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.direction.in_(("inbound", "outbound")),
            ConversationMessage.created_at >= marker.created_at,
        )
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(40)
    )).all())
    visible = [row for row in legacy_rows if row.actor_label != "پیام راهنمای تیم"]
    return list(reversed(visible[:8]))


async def _show_support(db: AsyncSession, conversation: Conversation, update: dict[str, Any], *, notice: str = "") -> None:
    messages = await _support_history(db, conversation)
    lines = []
    for item in messages:
        speaker = "تو" if item.direction == "inbound" else (item.actor_label or "تیم")
        content = " ".join(item.content.split())[:360]
        if content:
            lines.append(f"{speaker}: {content}")
    if lines:
        history = "\n".join(lines)
        body = f"گفتگوی پشتیبانی\n\n{history}\n\nپیامت رو همین‌جا بفرست؛ پاسخ تیم هم همین‌جا میاد."
    else:
        body = "پیامت رو همین‌جا بنویس؛ پاسخ تیم هم همین‌جا میاد."
    await _queue_ui(db, conversation, notice or body, _support_keyboard(), update)


async def _clear_support_messages(db: AsyncSession, conversation: Conversation, update: dict[str, Any]) -> int:
    session_id = _support_session_id(conversation)
    if not session_id:
        return 0
    session_field = ConversationMessage.metadata_json["support_session_id"].as_string()
    messages = list((await db.scalars(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.direction.in_(("inbound", "outbound")),
            ConversationMessage.provider_message_id.is_not(None),
            ConversationMessage.deleted_from_chat.is_(False),
            session_field == session_id,
        )
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
    )).all())
    existing_delete_ids = set((await db.scalars(
        select(OutboxMessage.provider_message_id).where(
            OutboxMessage.conversation_id == conversation.id,
            OutboxMessage.operation == "delete",
            OutboxMessage.provider_message_id.is_not(None),
        )
    )).all())
    seen: set[str] = set()
    deleted_requests = 0
    for message in messages:
        if message.actor_label == "پیام راهنمای تیم" or not message.provider_message_id:
            continue
        if message.provider_message_id in seen or message.provider_message_id in existing_delete_ids:
            continue
        seen.add(message.provider_message_id)
        await _queue_delete(db, conversation, message, update)
        deleted_requests += 1
    conversation.cleared_at = now_utc()
    return deleted_requests


def _message_kind(message: dict[str, Any]) -> str:
    keys = ("document", "photo", "audio", "video", "voice", "video_note", "animation", "sticker",
            "contact", "location", "venue", "poll", "dice", "game")
    return next((key for key in keys if key in message), "text")


def _message_metadata(message: dict[str, Any], kind: str) -> dict[str, Any]:
    raw = message.get(kind)
    if not isinstance(raw, dict):
        return {"kind": kind}
    allowed = ("title", "first_name", "last_name", "latitude", "longitude", "address", "phone_number",
               "emoji", "value", "duration", "file_size", "mime_type")
    return {key: str(raw[key])[:300] for key in allowed if key in raw and raw[key] is not None}


def _file_from_message(message: dict[str, Any]) -> tuple[str, str, str, int, str] | None:
    doc = message.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        return str(doc["file_id"]), str(doc.get("file_name") or "attachment"), str(doc.get("mime_type") or "application/octet-stream"), int(doc.get("file_size") or 0), "document"
    photos = message.get("photo")
    if isinstance(photos, list) and photos and isinstance(photos[-1], dict) and photos[-1].get("file_id"):
        photo = photos[-1]
        return str(photo["file_id"]), "image.jpg", "image/jpeg", int(photo.get("file_size") or 0), "photo"
    media_extensions = {"audio": ".mp3", "video": ".mp4", "voice": ".ogg", "video_note": ".mp4",
                        "animation": ".mp4", "sticker": ".webp"}
    for kind, suffix in media_extensions.items():
        media = message.get(kind)
        if isinstance(media, dict) and media.get("file_id"):
            mime = str(media.get("mime_type") or mimetypes.guess_type("file" + suffix)[0] or "application/octet-stream")
            name = str(media.get("file_name") or f"{kind}{suffix}")
            return str(media["file_id"]), name, mime, int(media.get("file_size") or 0), kind
    return None


async def _stored_bytes_for_conversation(db: AsyncSession, conversation_id: str) -> int:
    archived_keys = select(ConversationFile.stored_key).where(
        ConversationFile.conversation_id == conversation_id,
    )
    transcript_bytes = int(await db.scalar(select(func.coalesce(func.sum(ConversationFile.size_bytes), 0)).where(
        ConversationFile.conversation_id == conversation_id,
    )) or 0)
    project_bytes = int(await db.scalar(
        select(func.coalesce(func.sum(Attachment.size_bytes), 0))
        .join(Project, Project.id == Attachment.project_id)
        .where(Project.conversation_id == conversation_id, Attachment.stored_key.not_in(archived_keys))
    ) or 0)
    return transcript_bytes + project_bytes


async def document_budget(db: AsyncSession, conversation: Conversation) -> int:
    await db.execute(select(Conversation.id).where(Conversation.id == conversation.id).with_for_update())
    used = await _stored_bytes_for_conversation(db, conversation.id)
    limit = min(settings.max_upload_mb, 20) * 1024 * 1024
    return max(0, min(limit, settings.max_archive_mb * 1024 * 1024 - used))


async def queue_customer_message(db: AsyncSession, conversation: Conversation, admin: AdminUser,
                                  body: str, key: str, *, document: StoredUpload | None = None) -> ConversationMessage:
    conversation.updated_at = now_utc()
    session_field = ConversationMessage.metadata_json["support_session_id"].as_string()
    support_session_id = await db.scalar(
        select(session_field)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            session_field.is_not(None),
        )
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
        .limit(1)
    )
    metadata = {"delivery_status": "queued"}
    if support_session_id:
        metadata["support_session_id"] = str(support_session_id)
    caption = body[:MAX_DOCUMENT_CAPTION] if document else body[:4000]
    kind = "document" if document else "text"
    message = ConversationMessage(
        conversation_id=conversation.id, direction="outbound",
        actor_label=admin.display_name or admin.username,
        content=caption or (document.original_name if document else ""), message_kind=kind, metadata_json=metadata,
    )
    db.add(message)
    await db.flush()
    if document:
        db.add(ConversationFile(
            conversation_id=conversation.id, message_id=message.id, stored_key=document.stored_key,
            original_name=document.original_name, mime_type=document.mime_type,
            sha256=document.sha256, size_bytes=document.size_bytes,
        ))
    db.add(OutboxMessage(
        dedupe_key=key[:160], platform=conversation.platform, chat_id=conversation.chat_id,
        conversation_id=conversation.id, transcript_message_id=message.id, operation="document" if document else "send",
        body=caption, keyboard=None,
    ))
    return message


async def _handle_document(db: AsyncSession, adapter: BotAdapter, conversation: Conversation,
                           message: dict[str, Any], update: dict[str, Any], inbound: ConversationMessage,
                           identity: PlatformIdentity) -> None:
    draft = dict(conversation.draft_data or {})
    files = list(draft.get("files") or [])
    item = _file_from_message(message)
    if not item:
        return
    file_id, original_name, mime, provider_size, _media_kind = item
    archived_bytes = await _stored_bytes_for_conversation(db, conversation.id)
    archive_remaining = settings.max_archive_mb * 1024 * 1024 - archived_bytes
    download_limit = min(MAX_TOTAL_UPLOAD_BYTES, archive_remaining)
    if download_limit <= 0 or (provider_size and provider_size > download_limit):
        if conversation.state != "support":
            await _queue_delete(db, conversation, inbound, update)
        await _queue_ui(db, conversation, f"سقف ذخیرهٔ پیوست‌های این گفتگو ({settings.max_archive_mb} مگابایت) پر شده؛ متن و مشخصات پیام در آرشیو ثبت شد.",
                        _support_keyboard() if conversation.state == "support" else [[_button("منوی اصلی", "c:home")]], update)
        return
    used = sum(int(x.get("size_bytes", 0)) for x in files)
    try:
        path, provider_name, downloaded_mime, size = await adapter.download_to(
            file_id, settings.uploads_dir, download_limit,
        )
        stored = register_download(path, original_name or provider_name, mime or downloaded_mime)
    except (UploadRejected, ValueError) as exc:
        if conversation.state != "support":
            await _queue_delete(db, conversation, inbound, update)
        await _queue_ui(db, conversation, str(exc)[:250],
                        _support_keyboard() if conversation.state == "support" else
                        [[_button("ادامه بدون فایل", "c:skip_file")]], update)
        return
    except Exception as exc:
        logger.warning("attachment download failed platform=%s error=%s", adapter.platform, type(exc).__name__)
        if conversation.state != "support":
            await _queue_delete(db, conversation, inbound, update)
        await _queue_ui(db, conversation, "دریافت فایل انجام نشد. دوباره تلاش کن یا بدون فایل ادامه بده.",
                        _support_keyboard() if conversation.state == "support" else
                        [[_button("ادامه بدون فایل", "c:skip_file")]], update)
        return
    caption = inbound.content.strip()
    inbound.content = (caption + "\n" if caption else "") + f"[پیوست: {stored.original_name}]"
    db.add(ConversationFile(conversation_id=conversation.id, message_id=inbound.id,
                            stored_key=stored.stored_key, original_name=stored.original_name,
                            mime_type=stored.mime_type, sha256=stored.sha256, size_bytes=stored.size_bytes))
    if conversation.state != "support":
        await _queue_delete(db, conversation, inbound, update)
    if conversation.state == "attachment" and len(files) < MAX_FILES_PER_PROJECT and used + stored.size_bytes <= MAX_TOTAL_UPLOAD_BYTES:
        files.append({"stored_key": stored.stored_key, "original_name": stored.original_name,
                      "mime_type": stored.mime_type, "sha256": stored.sha256, "size_bytes": stored.size_bytes})
        draft["files"] = files
        conversation.draft_data = draft
        await _queue_ui(db, conversation, f"فایل {len(files)} از {MAX_FILES_PER_PROJECT} به درخواست پیوست شد.\nمی‌توانی فایل دیگری بفرستی یا به مرحلهٔ بعد بروی.",
                        [[_button("افزودن فایل دیگر", "c:stay_file"), _button("ادامه", "c:skip_file")],
                         [_button("لغو درخواست", "c:cancel", style="danger")]], update)
    elif conversation.state == "support" and "message" in update:
        await _notify_support_team(db, conversation, inbound)
        await _queue_ui(db, conversation, f"پیوست «{stored.original_name}» ثبت شد؛ توضیحت را هم همین‌جا بفرست.",
                        _support_keyboard(), update)
    elif conversation.state == "attachment":
        await _queue_ui(db, conversation, "فایل در آرشیو گفتگو ذخیره شد، اما به‌دلیل محدودیت تعداد یا حجم به درخواست پیوست نشد.",
                        [[_button("ادامه بدون فایل", "c:skip_file")], [_button("لغو درخواست", "c:cancel", style="danger")]], update)
    else:
        await _queue_ui(db, conversation, f"پیوست «{stored.original_name}» در آرشیو گفتگوی شما ثبت شد. برای پیوست‌کردن آن به پروژه، ثبت درخواست جدید را آغاز کن.",
                        [[_button("منوی اصلی", "c:home")]], update)


async def _staff_send_file(db: AsyncSession, adapter: BotAdapter, staff: AdminUser,
                           conversation: Conversation, message: dict[str, Any], update: dict[str, Any]) -> None:
    public_id = (conversation.draft_data or {}).get("delivery_project")
    query = _staff_project_scope(select(Project).where(Project.public_id == public_id), staff)
    project = await db.scalar(query.with_for_update()) if has_permission(staff.role, "messages.reply") else None
    customer = await db.get(Conversation, project.conversation_id) if project else None
    if not project or not customer or customer.customer_id != project.customer_id:
        await _staff_menu(db, conversation, update, staff, notice="این پروژه در دسترس نیست.")
        return
    keyboard = [[_button("لغو ارسال", "a:home")]]
    item = _file_from_message(message)
    if not item:
        await _queue_ui(db, conversation, "فایل را از گزینهٔ پیوست بفرستید.", keyboard, update)
        return
    file_id, name, mime, size, _ = item
    caption = str(message.get("caption") or "").strip()
    limit = await document_budget(db, customer)
    if not limit or size > limit or len(caption) > MAX_DOCUMENT_CAPTION:
        await _queue_ui(db, conversation, "حجم فایل یا طول توضیح بیشتر از سقف مجاز است.", keyboard, update)
        return
    path = None
    try:
        path, _, _, _ = await adapter.download_to(file_id, settings.uploads_dir, limit)
        document = register_download(path, name, mime)
    except (UploadRejected, OSError, ValueError, ProviderError):
        if path:
            path.unlink(missing_ok=True)
        await _queue_ui(db, conversation, "فایل دریافت نشد یا نوع آن مجاز نیست. دوباره امتحان کنید.", keyboard, update)
        return
    await queue_customer_message(db, customer, staff, caption or f"فایل پروژهٔ {project.public_id}",
                                 f"staff-document:{conversation.platform}:{update['update_id']}", document=document)
    db.add(ProjectEvent(project_id=project.id, actor_type="admin", actor_id=str(staff.id),
                        event_type="reply", note=document.original_name, private=False))
    await _staff_menu(db, conversation, update, staff, notice="فایل در صف ارسال به مشتری قرار گرفت.")


async def handle_update(db: AsyncSession, platform: str, update: dict[str, Any], adapter: BotAdapter) -> None:
    message, callback = _update_parts(update)
    if not message:
        return
    user = (callback.get("from") if callback else message.get("from")) or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    provider_user_id = str(user.get("id", ""))
    if not chat_id or not provider_user_id or str(chat.get("type", "")).casefold() != "private":
        return
    message_text = str(message.get("text", "")) if not callback else ""
    is_start, start_arg = _is_start(message_text)
    identity = await _identity(db, platform, user, chat_id, create_customer=not (is_start and start_arg.startswith("link_")))
    staff = await db.get(AdminUser, identity.admin_id) if identity.admin_id else None
    if staff and not staff.active:
        # Preserve the linked identity so the owner can restore bot access.
        return
    conversation = await _conversation(db, identity, platform, chat_id)

    if is_start and start_arg.startswith("link_"):
        linked = await _finish_admin_link(db, identity, platform, start_arg.removeprefix("link_"), conversation, update, user)
        if linked:
            staff = await db.get(AdminUser, identity.admin_id)
            await _staff_menu(db, conversation, update, staff, force_send=True,
                              notice="حساب کاری شما به این پیام‌رسان متصل شد.")
        else:
            await _queue_ui(db, conversation, "لینک اتصال نامعتبر، منقضی یا قبلاً استفاده شده است.",
                            [[_button("منوی اصلی", "c:home")]], update, force_send=True)
        return

    if not staff and identity.customer_id is None:
        identity = await _identity(db, platform, user, chat_id, create_customer=True)
        conversation.customer_id = identity.customer_id
    staff = await db.get(AdminUser, identity.admin_id) if identity.admin_id else None
    inbound = None
    if not callback and ("message" in update or "edited_message" in update):
        inbound = await _archive_inbound(db, conversation, update, message, user, edited="edited_message" in update)
        await db.flush()

    if callback:
        data = str(callback.get("data", ""))[:64]
        await _archive_event(db, conversation, _label(user), _callback_description(data), update)
        if data in {"c:home", "a:home"}:
            if not staff:
                if conversation.state in {"support", "clear_confirm"}:
                    await _clear_support_messages(db, conversation, update)
                await _remove_draft_files(conversation)
            await _show_home(db, conversation, update, staff=bool(staff), can_view_all=bool(staff and has_permission(staff.role, "projects.read")), can_report=bool(staff and has_permission(staff.role, "reports.read")))
            return
        if data == "c:new" and not staff and conversation.state == "home":
            conversation.state = "discipline"
            conversation.draft_data = {"files": []}
            await _queue_ui(db, conversation, "رشته‌ات رو انتخاب کن",
                            [[_button("کامپیوتر", "c:track:computer", style="primary")]], update)
            return
        if data == "c:track:computer" and not staff and conversation.state == "discipline":
            draft = dict(conversation.draft_data or {})
            draft["subject_area"] = "computer"
            conversation.draft_data = draft
            conversation.state = "title"
            await _queue_ui(db, conversation, "عنوان پروژه‌ات رو بنویس.",
                            [[_button("لغو درخواست", "c:cancel", style="danger")]], update)
            return
        if data == "c:projects" and not staff:
            await _project_list(db, conversation, update, identity.customer_id)
            return
        if data == "c:help" and not staff and conversation.state == "home":
            conversation.state = "support"
            conversation.draft_data = {"support_session_id": uuid4().hex}
            await _archive_event(db, conversation, "سیستم", "گفتگوی پشتیبانی باز شد.", update)
            await _show_support(db, conversation, update)
            return
        if data == "c:clear" and not staff and conversation.state == "support":
            conversation.state = "clear_confirm"
            await _queue_ui(db, conversation,
                            "پیام‌های این بخش تا جایی که پیام‌رسان اجازه بدهد پاک می‌شوند؛ سابقهٔ تیم باقی می‌ماند.",
                            [[_button("پاک کردن", "c:clear_yes", style="danger"),
                              _button("انصراف", "c:clear_cancel")]], update)
            return
        if data == "c:clear_yes" and not staff and conversation.state == "clear_confirm":
            await _clear_support_messages(db, conversation, update)
            conversation.state = "support"
            await _queue_ui(db, conversation,
                            "صفحه پاک شد. بعضی پیام‌های قدیمی ممکن است طبق محدودیت پیام‌رسان قابل حذف نباشند؛ آرشیو تیم حفظ شده است.",
                            _support_keyboard(), update)
            return
        if data == "c:clear_cancel" and not staff and conversation.state == "clear_confirm":
            conversation.state = "support"
            await _show_support(db, conversation, update)
            return
        if data == "c:cancel" and not staff:
            await _remove_draft_files(conversation)
            conversation.draft_data = {}
            await _show_home(db, conversation, update, notice="ثبت درخواست لغو شد؛ هر زمان بخواهی می‌توانی دوباره شروع کنی.")
            return
        if data in {"c:skip_file", "c:stay_file"} and not staff and conversation.state == "attachment":
            if data == "c:stay_file":
                await _queue_ui(db, conversation, "فایل بعدی را ارسال کن یا ادامه بده.",
                                [[_button("ادامه", "c:skip_file")], [_button("لغو درخواست", "c:cancel", style="danger")]], update)
            else:
                conversation.state = "deadline"
                await _queue_ui(db, conversation, "زمان تحویل مدنظرت چیه؟",
                                [[_button("رد شدن", "c:skip_deadline")], [_button("لغو درخواست", "c:cancel", style="danger")]], update)
            return
        if data == "c:skip_deadline" and not staff and conversation.state == "deadline":
            draft = dict(conversation.draft_data or {})
            draft["deadline_note"] = ""
            conversation.draft_data = draft
            conversation.state = "budget"
            await _queue_ui(db, conversation, "بودجهٔ تقریبی به تومان چقدره؟",
                            [[_button("رد شدن", "c:skip_budget")], [_button("لغو درخواست", "c:cancel", style="danger")]], update)
            return
        if data == "c:skip_budget" and not staff and conversation.state == "budget":
            draft = dict(conversation.draft_data or {})
            draft["budget_toman"] = None
            conversation.draft_data = draft
            conversation.state = "review"
            await _show_review(db, conversation, update)
            return
        if data.startswith("c:edit:") and not staff:
            field = data.removeprefix("c:edit:")
            mapping = {"title": "title", "description": "description", "deadline": "deadline_note", "budget": "budget_toman"}
            if field in mapping:
                conversation.state = field
                await _queue_ui(db, conversation, f"مقدار تازهٔ {mapping[field]} را ارسال کن.",
                                [[_button("لغو و بازگشت", "c:review")]], update)
                return
        if data == "c:review" and not staff:
            conversation.state = "review"
            await _show_review(db, conversation, update)
            return
        if data == "c:confirm" and not staff and conversation.state == "review":
            await _create_project(db, conversation, identity.customer_id, update, user)
            return
        if data.startswith("c:project:") and not staff:
            await _project_view(db, conversation, update, data.removeprefix("c:project:"), identity.customer_id)
            return
        if staff and data in {"a:stats:1", "a:stats:7", "a:stats:30"} and has_permission(staff.role, "reports.read"):
            days = int(data.rsplit(":", 1)[1])
            conversation.state = "home"
            conversation.draft_data = {}
            rows, total = await bot_stats(db, days)
            lines = [f"گزارش آماری · کاربران ثبت‌شده: {total}", "تاریخ | شروع‌کننده / ثبت‌کننده پروژه / کاربر جدید"]
            lines.extend(f"{row['day']} · {row['starts']} / {row['submitters']} / {row['new_users']}" for row in rows)
            await _queue_ui(db, conversation, "\n".join(lines), [[
                _button("امروز", "a:stats:1", style="primary"),
                _button("۷ روز", "a:stats:7"), _button("۳۰ روز", "a:stats:30"),
            ], [_button("منوی اصلی", "a:home")]], update)
            return
        if staff and (match := re.fullmatch(r"a:([ps])([ar]):([a-f0-9-]{36})", data)):
            kind, action, object_id = match.groups()
            permitted = has_permission(staff.role, "projects.review") if kind == "p" else (
                has_permission(staff.role, "customers.read") and has_permission(staff.role, "messages.reply")
            )
            if permitted and action == "r":
                conversation.state = "staff_reject"
                conversation.draft_data = {"review_kind": kind, "review_id": object_id}
                await _queue_ui(db, conversation, "دلیل رد را برای ارسال به مشتری بنویسید.",
                                [[_button("لغو", "a:home")]], update)
            else:
                decided = permitted and await _decide_preview(db, staff, kind, object_id, True, "")
                await _staff_menu(db, conversation, update, staff,
                                  notice="وضعیت به «در حال بررسی» تغییر کرد." if decided else "این مورد قبلاً بررسی شده یا در دسترس نیست.")
            return
        if staff and data in {"a:projects", "a:queue"}:
            conversation.state = "home"
            conversation.draft_data = {}
        if staff and data == "a:projects":
            await _project_list(db, conversation, update, identity.customer_id or "", staff=staff)
            return
        if staff and data == "a:queue" and has_permission(staff.role, "projects.read"):
            await _project_list(db, conversation, update, identity.customer_id or "", staff=staff, all_projects=True)
            return
        if staff and data.startswith("a:file:") and has_permission(staff.role, "messages.reply"):
            public_id = data.removeprefix("a:file:")
            project = await db.scalar(_staff_project_scope(select(Project).where(Project.public_id == public_id), staff))
            if project:
                conversation.state = "staff_file"
                conversation.draft_data = {"delivery_project": public_id}
                await _queue_ui(db, conversation, f"فایل پروژهٔ {public_id} را بفرستید. توضیح فایل اختیاری است.",
                                [[_button("لغو ارسال", "a:home")]], update)
                return
        if staff and data.startswith("a:project:"):
            await _project_view(db, conversation, update, data.removeprefix("a:project:"), staff=staff)
            return
        await _queue_ui(db, conversation, "این گزینه در دسترس نیست یا دسترسی لازم را نداری.",
                        [[_button("منوی اصلی", "a:home" if staff else "c:home")]], update)
        return

    if is_start or message_text.strip().casefold() in {"/menu", "/help"}:
        if inbound and inbound.content.startswith("/start link_"):
            inbound.content = "/start [لینک اتصال تیم]"
        if not staff:
            await _remove_draft_files(conversation)
        if message_text.strip().casefold() in {"/menu", "/help"} and inbound:
            await _queue_delete(db, conversation, inbound, update)
        if staff:
            await _staff_menu(db, conversation, update, staff)
        else:
            if conversation.state in {"support", "clear_confirm"}:
                await _clear_support_messages(db, conversation, update)
            first_name = str(user.get("first_name", "")).strip()[:60]
            await _show_home(db, conversation, update, greeting=first_name)
        return
    if message_text.strip().casefold() == "/cancel":
        if not staff and conversation.state in {"support", "clear_confirm"}:
            await _clear_support_messages(db, conversation, update)
        await _remove_draft_files(conversation)
        await _show_home(db, conversation, update, staff=bool(staff),
                         can_view_all=bool(staff and has_permission(staff.role, "projects.read")),
                         can_report=bool(staff and has_permission(staff.role, "reports.read")), notice="فرایند جاری لغو شد.")
        return
    if staff and conversation.state == "staff_reject" and "message" in update:
        reason = message_text.strip()
        if not reason or reason.startswith("/") or len(reason) > 700:
            await _queue_ui(db, conversation, "دلیل رد را در حداکثر ۷۰۰ نویسه بنویسید.",
                            [[_button("لغو", "a:home")]], update)
            return
        draft = conversation.draft_data or {}
        decided = await _decide_preview(db, staff, draft.get("review_kind", ""), draft.get("review_id", ""), False, reason)
        await _staff_menu(db, conversation, update, staff,
                          notice="رد شد و دلیل برای مشتری در صف ارسال قرار گرفت." if decided else "این مورد قبلاً بررسی شده یا در دسترس نیست.")
        return
    if staff and conversation.state == "staff_file" and "message" in update:
        await _staff_send_file(db, adapter, staff, conversation, message, update)
        return
    if staff:
        await _queue_ui(db, conversation, "از دکمه‌های منوی همکاران برای مشاهدهٔ پروژه‌ها استفاده کنید.",
                        _home_keyboard(True, has_permission(staff.role, "projects.read"), has_permission(staff.role, "reports.read")), update)
        return

    content = str(message.get("text") or message.get("caption") or "").strip()
    if conversation.state == "support" and content and not content.startswith("/") and not _file_from_message(message) and "message" in update:
        await _notify_support_team(db, conversation, inbound)
        await _queue_ui(db, conversation, "پیامت به تیم رسید؛ پاسخ رو همین‌جا می‌بینی.",
                        _support_keyboard(), update)
        return
    if conversation.state == "title" and content:
        if len(content) < 4 or len(content) > 180:
            await _queue_ui(db, conversation, "عنوان باید بین ۴ تا ۱۸۰ نویسه باشد. دوباره بنویس.",
                            [[_button("لغو درخواست", "c:cancel", style="danger")]], update)
            return
        draft = dict(conversation.draft_data or {})
        draft["title"] = content
        conversation.draft_data = draft
        conversation.state = "description"
        await _queue_delete(db, conversation, inbound, update)
        await _queue_ui(db, conversation, "کمی دربارهٔ پروژه و خروجی موردنظرت بگو.",
                        [[_button("لغو درخواست", "c:cancel", style="danger")]], update)
        return
    if conversation.state == "description" and content:
        if len(content) < 10 or len(content) > MAX_TEXT_CHARS:
            await _queue_ui(db, conversation, "توضیح باید بین ۱۰ تا ۵۰۰۰ نویسه باشد. دوباره بنویس.",
                            [[_button("لغو درخواست", "c:cancel", style="danger")]], update)
            return
        draft = dict(conversation.draft_data or {})
        draft["description"] = content
        conversation.draft_data = draft
        conversation.state = "attachment"
        await _queue_delete(db, conversation, inbound, update)
        await _queue_ui(db, conversation, "فایل مرتبطی داری؟ بفرست؛ یا «ادامه» رو بزن.",
                        [[_button("ادامه بدون فایل", "c:skip_file")], [_button("لغو درخواست", "c:cancel", style="danger")]], update)
        return
    if _file_from_message(message):
        await _handle_document(db, adapter, conversation, message, update, inbound, identity)
        return
    if conversation.state == "deadline" and content:
        draft = dict(conversation.draft_data or {})
        draft["deadline_note"] = content[:120]
        conversation.draft_data = draft
        conversation.state = "budget"
        await _queue_delete(db, conversation, inbound, update)
        await _queue_ui(db, conversation, "بودجهٔ تقریبی به تومان چقدره؟",
                        [[_button("رد شدن", "c:skip_budget")], [_button("لغو درخواست", "c:cancel", style="danger")]], update)
        return
    if conversation.state == "budget" and content:
        try:
            amount = _parse_budget(content)
        except ValueError as exc:
            await _queue_ui(db, conversation, str(exc),
                            [[_button("رد شدن", "c:skip_budget")], [_button("لغو درخواست", "c:cancel", style="danger")]], update)
            return
        draft = dict(conversation.draft_data or {})
        draft["budget_toman"] = amount
        conversation.draft_data = draft
        conversation.state = "review"
        await _queue_delete(db, conversation, inbound, update)
        await _show_review(db, conversation, update)
        return
    if conversation.state == "review":
        await _queue_ui(db, conversation, "پیش‌نمایش درخواست را با دکمه‌ها تأیید یا اصلاح کن.",
                        await _review_keyboard(), update)
        return
    await _queue_ui(db, conversation, "برای ثبت درخواست یا پیگیری، از گزینه‌های منو استفاده کن.",
                    _home_keyboard(), update)


async def _review_keyboard() -> list[list[dict[str, str]]]:
    return [
        [_button("تأیید و ثبت درخواست", "c:confirm", style="success")],
        [_button("ویرایش عنوان", "c:edit:title"), _button("ویرایش توضیح", "c:edit:description")],
        [_button("ویرایش زمان", "c:edit:deadline"), _button("ویرایش بودجه", "c:edit:budget")],
        [_button("لغو درخواست", "c:cancel", style="danger")],
    ]


async def _show_review(db: AsyncSession, conversation: Conversation, update: dict[str, Any]) -> None:
    draft = dict(conversation.draft_data or {})
    budget = draft.get("budget_toman")
    budget_text = f"{budget:,} تومان" if budget is not None else "اعلام نشده"
    text = (
        "پیش‌نمایش درخواست\n\n"
        f"رشته: {'کامپیوتر' if draft.get('subject_area') == 'computer' else 'اعلام‌نشده'}\n"
        f"عنوان: {draft.get('title', '—')}\n\n"
        f"توضیح: {draft.get('description', '—')[:1800]}\n\n"
        f"فایل‌ها: {len(draft.get('files') or [])}\n"
        f"زمان موردنظر: {draft.get('deadline_note') or 'اعلام نشده'}\n"
        f"بودجه: {budget_text}"
    )
    await _queue_ui(db, conversation, text, await _review_keyboard(), update)


async def _create_project(db: AsyncSession, conversation: Conversation, customer_id: str,
                          update: dict[str, Any], user: dict[str, Any]) -> None:
    draft = dict(conversation.draft_data or {})
    if not draft.get("title") or not draft.get("description"):
        await _show_home(db, conversation, update, notice="اطلاعات فرم کامل نبود؛ درخواست را دوباره ثبت کن.")
        return
    project = Project(
        public_id=f"CP-{uuid4().hex[:12].upper()}", customer_id=customer_id,
        conversation_id=conversation.id, title=str(draft["title"])[:180],
        subject_area=str(draft.get("subject_area", "computer"))[:40],
        description=str(draft["description"])[:MAX_TEXT_CHARS],
        deadline_note=str(draft.get("deadline_note", ""))[:120],
        budget_toman=draft.get("budget_toman"), status="submitted",
    )
    db.add(project)
    await db.flush()
    for item in draft.get("files") or []:
        db.add(Attachment(project_id=project.id, stored_key=item["stored_key"], original_name=item["original_name"],
                          mime_type=item["mime_type"], sha256=item["sha256"], size_bytes=int(item["size_bytes"])))
    db.add(ProjectEvent(project_id=project.id, actor_type="customer", actor_id=customer_id,
                        event_type="submitted", note="درخواست از طریق گفتگوی خصوصی ثبت شد.", private=False))
    notified: set[tuple[int, str]] = set()
    staff_rows = (await db.execute(
        select(PlatformIdentity, AdminUser).join(AdminUser, AdminUser.id == PlatformIdentity.admin_id)
        .where(AdminUser.active.is_(True)).order_by(PlatformIdentity.platform, PlatformIdentity.id)
    )).all()
    for staff_identity, staff_member in staff_rows:
        can_review_all = has_permission(staff_member.role, "projects.review")
        assigned = project.assigned_admin_id == staff_member.id
        if not (can_review_all or assigned):
            continue
        key = (staff_member.id, staff_identity.platform)
        if key not in notified:
            await _queue_staff_notice(db, staff_identity, staff_member, project)
            notified.add(key)
    db.add(ConversationMessage(
        conversation_id=conversation.id, direction="event", message_kind="project_submitted",
        actor_label=_label(user), content=f"درخواست {project.public_id} · {project.title}\n{project.description}",
        metadata_json={"project_id": project.id},
    ))
    conversation.state = "home"
    conversation.draft_data = {}
    await _queue_ui(db, conversation,
                    f"درخواستت با شناسهٔ {project.public_id} ثبت شد. تیم آن را بررسی می‌کند؛ برای پیگیری از منوی اصلی استفاده کن.",
                    [[_button("پیگیری همین درخواست", f"c:project:{project.public_id}")],
                     [_button("منوی اصلی", "c:home")]], update)


async def _remove_draft_files(conversation: Conversation) -> None:
    # A submitted file is part of the archived conversation. Canceling an intake
    # draft must not erase that retained transcript attachment.
    return None
