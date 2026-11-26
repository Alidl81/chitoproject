from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.files import (
    MAX_DOCUMENT_CAPTION,
    StoredUpload,
    UploadRejected,
    safe_filename,
    save_document,
)
from app.bot.flow import document_budget
from app.bot.flow import queue_customer_message as _queue_customer_message
from app.bot.review import dismiss_previews
from app.config import settings
from app.database import get_session
from app.models import (
    AdminInvite,
    AdminSession,
    AdminUser,
    Attachment,
    AuditEvent,
    BotLinkChallenge,
    Broadcast,
    Conversation,
    ConversationFile,
    ConversationHidden,
    ConversationMessage,
    ConversationRead,
    Customer,
    OutboxMessage,
    PlatformIdentity,
    Project,
    ProjectEvent,
    new_id,
)
from app.permissions import Role, has_permission, role_label
from app.security import (
    constant_time_token_match,
    digest_token,
    expires_in,
    hash_password,
    now_utc,
    random_token,
    verify_password,
)
from app.stats import bot_stats, tehran_today
from app.web.chat_queries import important_history, unread_messages
from app.web.common import current_session, redirect, render, valid_session_csrf

router = APIRouter(prefix="/admin", tags=["admin"])
STATUS_LABELS = {
    "submitted": "ثبت‌شده",
    "reviewing": "در حال بررسی",
    "needs_info": "نیازمند اطلاعات",
    "in_progress": "در حال انجام",
    "completed": "تکمیل‌شده",
    "declined": "پذیرفته‌نشده",
    "canceled": "لغوشده",
}
STATUS_TRANSITIONS = {
    "submitted": {"reviewing", "needs_info", "declined", "canceled"},
    "reviewing": {"needs_info", "in_progress", "declined", "canceled"},
    "needs_info": {"submitted", "reviewing", "canceled"},
    "in_progress": {"completed", "canceled"},
    "completed": set(),
    "declined": set(),
    "canceled": set(),
}


async def _admin(request: Request, db: AsyncSession) -> tuple[AdminUser | None, AdminSession | None]:
    auth = await current_session(request, db)
    if not auth:
        return None, None
    admin = await db.get(AdminUser, auth.admin_id)
    return (admin, auth) if admin and admin.active else (None, None)


async def _guard(request: Request, db: AsyncSession, permission: str | None = None,
                 *, post: bool = False) -> tuple[AdminUser, AdminSession] | HTMLResponse | RedirectResponse:
    admin, auth = await _admin(request, db)
    if not admin or not auth:
        return redirect("/admin/login", "sign-in")
    permitted = bool(not permission or has_permission(admin.role, permission))
    if permission == "projects.read" and has_permission(admin.role, "projects.assigned.read"):
        permitted = True
    if permission and not permitted:
        return render(request, "error.html", admin=admin, status_code=403, title="دسترسی مجاز نیست",
                      message="این بخش برای نقش کاربری شما فعال نیست.", csrf_token=auth.csrf_token)
    if post:
        form = await request.form()
        if not valid_session_csrf(auth, str(form.get("csrf_token", ""))):
            return render(request, "error.html", admin=admin, status_code=403, title="درخواست معتبر نیست",
                          message="فرم منقضی شده است. صفحه را تازه کنید و دوباره بفرستید.", csrf_token=auth.csrf_token)
    return admin, auth


def _page(request: Request, name: str, admin: AdminUser, auth: AdminSession,
          *, status_code: int = 200, **context) -> HTMLResponse:
    return render(request, name, admin=admin, csrf_token=auth.csrf_token,
                  notice=request.query_params.get("notice", ""), status_code=status_code, **context)


def _audit(db: AsyncSession, admin: AdminUser, action: str, object_type: str = "", object_id: str = "",
           detail: dict | None = None) -> None:
    db.add(AuditEvent(actor_admin_id=admin.id, action=action, object_type=object_type,
                      object_id=object_id, detail=detail or {}))


async def _project_scope(db: AsyncSession, admin: AdminUser, project_id: str) -> Project | None:
    project = await db.get(Project, project_id)
    if not project:
        return None
    if admin.role == Role.CONTRIBUTOR.value and project.assigned_admin_id != admin.id:
        return None
    return project


async def _queue_assignee_notice(db: AsyncSession, project: Project, member: AdminUser) -> None:
    identities = list((await db.scalars(select(PlatformIdentity).where(
        PlatformIdentity.admin_id == member.id, PlatformIdentity.platform.in_(["telegram", "bale"])
    ))).all())
    for identity in identities:
        conversation = await db.scalar(select(Conversation).where(
            Conversation.platform == identity.platform, Conversation.chat_id == identity.chat_id,
        ))
        if conversation is None:
            conversation = Conversation(platform=identity.platform, chat_id=identity.chat_id,
                                        admin_id=member.id, state="home", draft_data={})
            db.add(conversation)
            await db.flush()
        body = f"پروژهٔ {project.public_id} به شما واگذار شد.\n{project.title[:180]}"
        transcript = ConversationMessage(
            conversation_id=conversation.id, direction="outbound", actor_label="اعلان واگذاری",
            content=body, metadata_json={"delivery_status": "queued", "project_id": project.id},
        )
        db.add(transcript)
        await db.flush()
        db.add(OutboxMessage(
            dedupe_key=f"assignment:{project.id}:{member.id}:{identity.platform}:{random_token(8)}"[:160],
            platform=identity.platform, chat_id=identity.chat_id, conversation_id=conversation.id,
            transcript_message_id=transcript.id, operation="send", body=body,
            keyboard=[[{"text": "بازکردن پروژه", "url":
                        f"{settings.public_base_url.rstrip('/')}/admin/projects/{project.id}"}]],
        ))


@router.get("")
async def dashboard(request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    project_access = has_permission(admin.role, "projects.read") or has_permission(admin.role, "projects.assigned.read")
    chat_access = has_permission(admin.role, "customers.read")
    if not project_access and not chat_access:
        return _page(request, "error.html", admin, auth, status_code=403,
                     title="دسترسی مجاز نیست", message="برای حساب شما بخش مدیریتی فعالی تعریف نشده است.")
    unread = int(await db.scalar(unread_messages(admin.id)) or 0) if chat_access else None
    scope = [Project.assigned_admin_id == admin.id] if admin.role == Role.CONTRIBUTOR.value else []
    if not project_access:
        scope = [Project.id.is_(None)]
    total = int(await db.scalar(select(func.count(Project.id)).where(*scope)) or 0)
    counts = {status: int(await db.scalar(select(func.count(Project.id)).where(Project.status == status, *scope)) or 0)
              for status in STATUS_LABELS}
    active_projects = int(await db.scalar(select(func.count(Project.id)).where(
        Project.status.in_(["submitted", "reviewing", "needs_info", "in_progress"]), *scope)) or 0)
    total_customers = int(await db.scalar(select(func.count(func.distinct(Project.customer_id))).where(*scope)) or 0)
    recent = list((await db.scalars(select(Project).where(*scope).order_by(Project.updated_at.desc()).limit(8))).all())
    members = int(await db.scalar(select(func.count(AdminUser.id)).where(AdminUser.active.is_(True))) or 0) if has_permission(admin.role, "team.invite") else None
    return _page(request, "dashboard.html", admin, auth, total=total, counts=counts,
                 active_projects=active_projects, total_customers=total_customers,
                 recent=recent, status_labels=STATUS_LABELS, members=members, unread=unread)


@router.get("/stats")
async def stats_page(request: Request, db: AsyncSession = Depends(get_session),
                     days: int = 7, since: date | None = None, through: date | None = None):
    guard = await _guard(request, db, "reports.read")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    today = tehran_today()
    through = through or today
    if through.year < 2000:
        return redirect("/admin/stats", "stats-invalid-range")
    if since is None:
        since = through - timedelta(days=max(1, min(days, 90)) - 1)
    days = (through - since).days + 1
    if through > today or since.year < 2000 or not 1 <= days <= 90:
        return redirect("/admin/stats", "stats-invalid-range")
    rows, total = await bot_stats(db, days, through)
    peak = max((max(row["starts"], row["submitters"], row["new_users"]) for row in rows), default=0)
    return _page(request, "stats.html", admin, auth, stats=rows, total_users=total, since=since, through=through, today=today, peak=max(1, peak))


@router.get("/projects")
async def projects_page(request: Request, db: AsyncSession = Depends(get_session),
                        status: str = "", q: str = "", page: int = 1):
    guard = await _guard(request, db, "projects.read")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    query = select(Project)
    if admin.role == Role.CONTRIBUTOR.value:
        query = query.where(Project.assigned_admin_id == admin.id)
    if status in STATUS_LABELS:
        query = query.where(Project.status == status)
    if q.strip():
        needle = f"%{q.strip()[:80]}%"
        query = query.where(or_(Project.title.ilike(needle), Project.public_id.ilike(needle)))
    total = int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    page_count = max(1, (total + 29) // 30)
    page = min(max(page, 1), page_count)
    items = list((await db.scalars(query.order_by(Project.updated_at.desc()).offset((page - 1) * 30).limit(30))).all())
    return _page(request, "projects.html", admin, auth, items=items, total=total,
                 status_labels=STATUS_LABELS, selected_status=status, search=q, page=page,
                 page_count=page_count)


@router.get("/projects/{project_id}")
async def project_detail(project_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db, "projects.read")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    project = await _project_scope(db, admin, project_id)
    if not project:
        return _page(request, "error.html", admin, auth, status_code=404,
                     title="پروژه پیدا نشد", message="این پروژه در دسترس نیست.")
    customer = await db.get(Customer, project.customer_id)
    conversation = await db.get(Conversation, project.conversation_id)
    transcript = list((await db.scalars(select(ConversationMessage).where(
        ConversationMessage.conversation_id == project.conversation_id, important_history()).order_by(
            ConversationMessage.created_at.desc(), ConversationMessage.revision.desc()).limit(300))).all())
    transcript.reverse()
    events = list((await db.scalars(select(ProjectEvent).where(ProjectEvent.project_id == project.id)
                                    .order_by(ProjectEvent.created_at.desc()).limit(100))).all())
    events.reverse()
    attachments = list((await db.scalars(select(Attachment).where(Attachment.project_id == project.id)
                                         .order_by(Attachment.created_at))).all())
    transcript_files = list((await db.scalars(select(ConversationFile).where(
        ConversationFile.message_id.in_([message.id for message in transcript]),
        ConversationFile.conversation_id == project.conversation_id,
    ))).all())
    members = list((await db.scalars(select(AdminUser).where(AdminUser.active.is_(True)).order_by(AdminUser.display_name, AdminUser.username))).all()) if has_permission(admin.role, "projects.manage") else []
    return _page(request, "project_detail.html", admin, auth, project=project, customer=customer,
                 conversation=conversation, transcript=transcript, transcript_files=transcript_files, events=events, attachments=attachments,
                 members=members, status_labels=STATUS_LABELS, can_manage=has_permission(admin.role, "projects.manage"),
                 can_reply=has_permission(admin.role, "messages.reply"), can_review=has_permission(admin.role, "projects.review"))


@router.post("/projects/{project_id}/status")
async def project_status(project_id: str, request: Request, db: AsyncSession = Depends(get_session),
                         target: str = Form(""), note: str = Form("")):
    guard = await _guard(request, db, "projects.review", post=True)
    if not isinstance(guard, tuple):
        if isinstance(guard, RedirectResponse):
            return guard
        return guard
    admin, auth = guard
    project = await db.get(Project, project_id, with_for_update=True)
    if not project:
        return _page(request, "error.html", admin, auth, status_code=404, title="پروژه پیدا نشد", message="این پروژه در دسترس نیست.")
    allowed = STATUS_TRANSITIONS.get(project.status, set())
    if admin.role == Role.REVIEWER.value:
        allowed &= {"reviewing", "needs_info", "declined"}
    if target not in allowed:
        return redirect(f"/admin/projects/{project.id}", "invalid-transition")
    old = project.status
    project.status = target
    project.updated_at = now_utc()
    db.add(ProjectEvent(project_id=project.id, actor_type="admin", actor_id=str(admin.id), event_type="status",
                        note=note.strip()[:1000], private=False))
    conversation = await db.get(Conversation, project.conversation_id)
    if conversation:
        text = f"وضعیت درخواست {project.public_id} به «{STATUS_LABELS[target]}» تغییر کرد."
        if note.strip():
            text += f"\n\nیادداشت تیم: {note.strip()[:700]}"
        await _queue_customer_message(db, conversation, admin, text, f"status:{project.id}:{old}:{target}:{int(now_utc().timestamp())}")
    if old == "submitted":
        await dismiss_previews(db, f"new-project:{project.id}")
    _audit(db, admin, "project.status", "project", project.id, {"from": old, "to": target})
    await db.commit()
    return redirect(f"/admin/projects/{project.id}", "status-saved")


@router.post("/projects/{project_id}/assign")
async def project_assign(project_id: str, request: Request, db: AsyncSession = Depends(get_session),
                         admin_id: int = Form(0)):
    guard = await _guard(request, db, "projects.manage", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    project = await _project_scope(db, admin, project_id)
    member = await db.get(AdminUser, admin_id)
    if not project or not member or not member.active:
        return redirect("/admin/projects", "invalid-assignee")
    project.assigned_admin_id = member.id
    project.updated_at = now_utc()
    db.add(ProjectEvent(project_id=project.id, actor_type="admin", actor_id=str(admin.id), event_type="assigned",
                        note=f"واگذار شد به {member.display_name or member.username}", private=True))
    _audit(db, admin, "project.assign", "project", project.id, {"admin_id": member.id})
    await _queue_assignee_notice(db, project, member)
    await db.commit()
    return redirect(f"/admin/projects/{project.id}", "assigned")


async def _save_reply_document(db: AsyncSession, conversation: Conversation, file: UploadFile | None,
                               caption: str) -> StoredUpload | None:
    if not file or not file.filename:
        return None
    limit = await document_budget(db, conversation)
    if limit <= 0 or len(caption) > MAX_DOCUMENT_CAPTION:
        raise UploadRejected()
    return await asyncio.to_thread(save_document, file.file, file.filename, limit)


async def _commit_reply(db: AsyncSession, document: StoredUpload | None) -> None:
    try:
        await db.commit()
    except Exception:
        if document:
            document.path.unlink(missing_ok=True)
        raise


@router.post("/projects/{project_id}/reply")
async def project_reply(project_id: str, request: Request, db: AsyncSession = Depends(get_session),
                        body: str = Form(""), file: UploadFile | None = File(None)):
    guard = await _guard(request, db, "messages.reply", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    project = await _project_scope(db, admin, project_id)
    text = body.strip()
    if not project or (not text and not (file and file.filename)):
        return redirect(f"/admin/projects/{project_id}", "empty-reply")
    conversation = await db.get(Conversation, project.conversation_id)
    if not conversation or conversation.customer_id != project.customer_id:
        return redirect(f"/admin/projects/{project.id}", "conversation-missing")
    target = f"/admin/projects/{project.id}"
    try:
        document = await _save_reply_document(db, conversation, file, text)
    except UploadRejected:
        return redirect(target, "reply-file-rejected")
    await _queue_customer_message(db, conversation, admin, text, f"admin-reply:{project.id}:{random_token(12)}", document=document)
    db.add(ProjectEvent(project_id=project.id, actor_type="admin", actor_id=str(admin.id), event_type="reply",
                        note=(text or (document.original_name if document else ""))[:1000], private=False))
    _audit(db, admin, "project.reply", "project", project.id, {})
    await _commit_reply(db, document)
    return redirect(f"/admin/projects/{project.id}", "reply-queued")


@router.post("/projects/{project_id}/note")
async def project_note(project_id: str, request: Request, db: AsyncSession = Depends(get_session),
                       body: str = Form("")):
    guard = await _guard(request, db, "projects.manage", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    project = await _project_scope(db, admin, project_id)
    text = body.strip()
    if not project or not text:
        return redirect(f"/admin/projects/{project_id}", "empty-note")
    db.add(ProjectEvent(project_id=project.id, actor_type="admin", actor_id=str(admin.id), event_type="note",
                        note=text[:4000], private=True))
    _audit(db, admin, "project.private_note", "project", project.id, {})
    await db.commit()
    return redirect(f"/admin/projects/{project.id}", "note-saved")


def _escaped_search(value: str) -> str:
    return value.replace("/", "//").replace("%", "/%").replace("_", "/_")


def _conversation_identity():
    return (
        select(PlatformIdentity.id)
        .where(
            PlatformIdentity.customer_id == Conversation.customer_id,
            PlatformIdentity.platform == Conversation.platform,
            PlatformIdentity.chat_id == Conversation.chat_id,
        )
        .order_by(PlatformIdentity.last_seen_at.desc(), PlatformIdentity.id.desc())
        .limit(1).correlate(Conversation).scalar_subquery()
    )


@router.get("/conversations")
async def conversations_page(request: Request, db: AsyncSession = Depends(get_session),
                             q: str = "", page: int = 1, archived: bool = False, logs: bool = False):
    guard = await _guard(request, db, "customers.read")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    search = q.strip()[:80]
    query = (
        select(Conversation, Customer, PlatformIdentity,
               unread_messages(admin.id, conversation_id=Conversation.id).correlate(Conversation).scalar_subquery())
        .join(Customer, Customer.id == Conversation.customer_id)
        .outerjoin(PlatformIdentity, PlatformIdentity.id == _conversation_identity())
        .outerjoin(ConversationHidden, and_(
            ConversationHidden.conversation_id == Conversation.id,
            ConversationHidden.admin_id == admin.id,
        ))
    )
    hidden = or_(
        and_(Conversation.panel_hidden_at.is_not(None), Conversation.panel_hidden_at >= Conversation.updated_at),
        and_(ConversationHidden.hidden_at.is_not(None), ConversationHidden.hidden_at >= Conversation.updated_at),
    )
    if not logs:
        query = query.where(hidden if archived else ~hidden)
    if search:
        term = search.lstrip("@").replace("/", "//").replace("%", "/%").replace("_", "/_")
        needle = f"%{term}%"
        matches = [column.ilike(needle, escape="/") for column in (
            Customer.display_name, PlatformIdentity.username, PlatformIdentity.provider_user_id, Conversation.chat_id,
        )]
        if logs:
            raw = "%" + _escaped_search(search) + "%"
            matches.append(select(ConversationMessage.id).where(
                ConversationMessage.conversation_id == Conversation.id,
                ConversationMessage.content.ilike(raw, escape="/"),
            ).correlate(Conversation).exists())
        query = query.where(or_(*matches))
    total = int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    page_count = max(1, (total + 29) // 30)
    page = min(max(page, 1), page_count)
    items = (await db.execute(
        query.order_by(func.lower(Customer.display_name), Customer.id, Conversation.platform, Conversation.id)
        .offset((page - 1) * 30).limit(30)
    )).all()
    return _page(request, "conversations.html", admin, auth, items=items, total=total, page=page,
                 page_count=page_count, search=search, archived=archived, logs=logs, is_owner=admin.role == Role.OWNER.value)


@router.post("/conversations/{conversation_id}/hide")
async def hide_conversation(conversation_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db, "customers.read", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, _ = guard
    conversation = await db.get(Conversation, conversation_id, with_for_update=True)
    if not conversation or not conversation.customer_id:
        return redirect("/admin/conversations", "missing")
    hidden = await db.get(ConversationHidden, (admin.id, conversation.id))
    if hidden is None:
        db.add(ConversationHidden(admin_id=admin.id, conversation_id=conversation.id, hidden_at=now_utc()))
    else:
        hidden.hidden_at = now_utc()
    await db.commit()
    return redirect("/admin/conversations", "conversation-hidden")


@router.post("/conversations/{conversation_id}/remove-both")
async def remove_conversation_both(conversation_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db, "*", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, _ = guard
    conversation = await db.get(Conversation, conversation_id, with_for_update=True)
    if not conversation or not conversation.customer_id or conversation.admin_id is not None:
        return redirect("/admin/conversations", "missing")
    now = now_utc()
    eligible = select(ConversationMessage.provider_message_id).where(
        ConversationMessage.conversation_id == conversation.id,
        ConversationMessage.provider_message_id.is_not(None),
        ConversationMessage.provider_message_id != "0",
        ConversationMessage.direction.in_(("inbound", "outbound")),
        ConversationMessage.deleted_from_chat.is_(False),
    )
    if conversation.platform == "telegram":
        # An edited message's archive timestamp can be newer than the original.
        eligible = eligible.where(ConversationMessage.created_at >= now - timedelta(hours=48),
                                  ConversationMessage.revision == 1)
    eligible = eligible.distinct()
    existing = select(OutboxMessage.provider_message_id).where(
        OutboxMessage.conversation_id == conversation.id, OutboxMessage.operation == "delete",
        OutboxMessage.status.in_(("pending", "processing", "sent")), OutboxMessage.provider_message_id.is_not(None),
    )
    ids = list(await db.scalars(eligible.where(ConversationMessage.provider_message_id.not_in(existing))))
    streak = random_token(12)
    for index, provider_id in enumerate(ids):
        db.add(OutboxMessage(
            dedupe_key=f"panel-delete:{streak}:{index}", platform=conversation.platform, chat_id=conversation.chat_id,
            conversation_id=conversation.id, operation="delete", provider_message_id=provider_id, body="",
        ))
    # Assign both timestamps explicitly so onupdate cannot make the conversation visible again.
    conversation.updated_at = now
    conversation.panel_hidden_at = now
    _audit(db, admin, "conversation.remove_both", "conversation", conversation.id, {"queued": len(ids)})
    await db.commit()
    return redirect("/admin/conversations", "conversation-delete-queued" if ids else "conversation-no-deletable")


def _broadcast_channels() -> dict[str, str]:
    channels = {}
    if settings.telegram_bot_token:
        channels["telegram"] = "تلگرام"
    if settings.bale_bot_token:
        channels["bale"] = "بله"
    return channels


def _broadcast_recipients(platforms: list[str]):
    # Keep customer conversations only, including those hidden from a colleague's list.
    linked_staff = select(PlatformIdentity.id).where(
        PlatformIdentity.platform == Conversation.platform,
        PlatformIdentity.chat_id == Conversation.chat_id,
        PlatformIdentity.admin_id.is_not(None),
    ).exists()
    return select(Conversation.id, Conversation.platform, Conversation.chat_id).where(
        Conversation.platform.in_(platforms), Conversation.customer_id.is_not(None),
        Conversation.admin_id.is_(None), ~linked_staff,
    )


@router.get("/broadcasts")
async def broadcasts_page(request: Request, db: AsyncSession = Depends(get_session), batch: str = ""):
    guard = await _guard(request, db, "*")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    history = list(await db.scalars(select(Broadcast).order_by(Broadcast.created_at.desc()).limit(10)))
    active = await db.get(Broadcast, batch) if re.fullmatch(r"[a-f0-9-]{36}", batch) else None
    counts = {}
    if active:
        counts = dict((await db.execute(select(OutboxMessage.status, func.count()).where(
            OutboxMessage.dedupe_key.startswith(f"broadcast:{active.id}:")
        ).group_by(OutboxMessage.status))).all())
    return _page(request, "broadcasts.html", admin, auth, channels=_broadcast_channels(),
                 history=history, active=active, counts=counts)


@router.post("/broadcasts")
async def broadcast_submit(request: Request, db: AsyncSession = Depends(get_session),
                           body: str = Form(""), audience: str = Form("all"),
                           batch: str = Form(""), confirmation: str = Form("")):
    guard = await _guard(request, db, "*", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    channels = _broadcast_channels()
    selected = list(channels) if audience == "all" else [audience] if audience in channels else []
    body = body.strip()
    if not selected or not 0 < len(body) <= 4000:
        return redirect("/admin/broadcasts", "broadcast-invalid")
    recipients = _broadcast_recipients(selected)
    if batch and not re.fullmatch(r"[a-f0-9-]{36}", batch):
        return redirect("/admin/broadcasts", "broadcast-invalid")
    if batch and await db.get(Broadcast, batch):
        return redirect(f"/admin/broadcasts?batch={batch}")
    rows = (await db.execute(recipients.order_by(Conversation.id))).all()
    if not rows:
        return redirect("/admin/broadcasts", "broadcast-empty")
    preview_id = batch or new_id()
    snapshot = json.dumps(["broadcast-preview", auth.csrf_token, preview_id, audience, body,
                           [tuple(row) for row in rows]], ensure_ascii=False)
    if not batch:
        return _page(request, "broadcasts.html", admin, auth, preview=body, audience=audience,
                     count=len(rows), batch=preview_id, confirmation=digest_token(snapshot), channels=channels)
    if not constant_time_token_match(snapshot, confirmation):
        return redirect("/admin/broadcasts", "broadcast-audience-changed")
    now = now_utc()
    db.add(Broadcast(id=batch, admin_id=admin.id, body=body, audience=audience, recipient_count=len(rows)))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        if await db.get(Broadcast, batch):
            return redirect(f"/admin/broadcasts?batch={batch}")
        raise
    for start in range(0, len(rows), 250):
        messages, jobs = [], []
        for index, (conversation_id, platform, chat_id) in enumerate(rows[start:start + 250], start):
            message_id = new_id()
            messages.append(dict(id=message_id, conversation_id=conversation_id, direction="outbound",
                                 actor_label=admin.display_name or admin.username, content=body,
                                 metadata_json={"delivery_status": "queued", "broadcast_id": batch}))
            jobs.append(dict(dedupe_key=f"broadcast:{batch}:{conversation_id}", platform=platform,
                             chat_id=chat_id, conversation_id=conversation_id, transcript_message_id=message_id,
                             operation="send", body=body, next_attempt_at=now + timedelta(seconds=index // 10)))
        await db.execute(insert(ConversationMessage), messages)
        await db.execute(insert(OutboxMessage), jobs)
    _audit(db, admin, "broadcast.queued", "broadcast", batch, {"audience": audience, "recipients": len(rows)})
    await db.commit()
    return redirect(f"/admin/broadcasts?batch={batch}")


@router.get("/conversations/{conversation_id}")
async def conversation_detail(conversation_id: str, request: Request, db: AsyncSession = Depends(get_session),
                             page: int = 1, logs: bool = False, q: str = ""):
    guard = await _guard(request, db, "customers.read")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    conversation = await db.get(Conversation, conversation_id, with_for_update=not logs and page == 1)
    if not conversation or not conversation.customer_id:
        return _page(request, "error.html", admin, auth, status_code=404, title="گفتگو پیدا نشد", message="این گفتگو در دسترس نیست.")
    project = await db.scalar(select(Project).where(Project.conversation_id == conversation.id).order_by(Project.created_at.desc()))
    if admin.role == Role.CONTRIBUTOR.value and (not project or project.assigned_admin_id != admin.id):
        return _page(request, "error.html", admin, auth, status_code=403, title="دسترسی مجاز نیست", message="این گفتگو به پروژهٔ واگذارشدهٔ شما متصل نیست.")
    search = q.strip()[:100] if logs else ""
    filters = [ConversationMessage.conversation_id == conversation.id]
    if not logs:
        filters.append(important_history())
    elif search:
        filters.append(ConversationMessage.content.ilike(f"%{_escaped_search(search)}%", escape="/"))
    shown_at = now_utc()
    total_messages = int(await db.scalar(select(func.count(ConversationMessage.id)).where(*filters)) or 0)
    page_count = max(1, (total_messages + 99) // 100)
    page = min(max(page, 1), page_count)
    messages = list((await db.scalars(select(ConversationMessage).where(*filters).order_by(
        ConversationMessage.created_at.desc(), ConversationMessage.revision.desc(), ConversationMessage.id.desc()
    ).offset((page - 1) * 100).limit(100))).all())
    messages.reverse()
    files = list((await db.scalars(select(ConversationFile).where(
        ConversationFile.conversation_id == conversation.id,
        ConversationFile.message_id.in_([message.id for message in messages]),
    ).order_by(ConversationFile.created_at))).all())
    mark_read = not logs and page == 1
    if mark_read:
        # Serialize updates to this colleague's read marker with the conversation row.
        await db.execute(select(Conversation.id).where(Conversation.id == conversation.id).with_for_update())
        marker = await db.get(ConversationRead, (admin.id, conversation.id))
        if marker is None:
            db.add(ConversationRead(admin_id=admin.id, conversation_id=conversation.id, read_at=shown_at))
        else:
            marker.read_at = shown_at
    customer = await db.get(Customer, conversation.customer_id)
    identity = await db.scalar(select(PlatformIdentity).where(
        PlatformIdentity.customer_id == conversation.customer_id,
        PlatformIdentity.platform == conversation.platform,
        PlatformIdentity.chat_id == conversation.chat_id,
    ).order_by(PlatformIdentity.last_seen_at.desc(), PlatformIdentity.id.desc()).limit(1))
    remote_removal = dict((await db.execute(
        select(OutboxMessage.status, func.count()).where(
            OutboxMessage.conversation_id == conversation.id, OutboxMessage.dedupe_key.startswith("panel-delete:"),
        ).group_by(OutboxMessage.status)
    )).all())
    if mark_read:
        await db.commit()
    return _page(request, "conversation_detail.html", admin, auth,
                 conversation=conversation, customer=customer, identity=identity, remote_removal=remote_removal,
                 project=project, messages=messages, files=files,
                 page=page, page_count=page_count, total_messages=total_messages, logs=logs, search=search,
                 can_delete=admin.role == Role.OWNER.value,
                 can_reply=has_permission(admin.role, "messages.reply"))


@router.post("/conversations/{conversation_id}/reply")
async def conversation_reply(conversation_id: str, request: Request, db: AsyncSession = Depends(get_session),
                             body: str = Form(""), file: UploadFile | None = File(None)):
    guard = await _guard(request, db, "messages.reply", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    conversation = await db.get(Conversation, conversation_id)
    text = body.strip()
    if not conversation or not conversation.customer_id or (not text and not (file and file.filename)):
        return redirect(f"/admin/conversations/{conversation_id}", "empty-reply")
    project = await db.scalar(select(Project).where(Project.conversation_id == conversation.id))
    if admin.role == Role.CONTRIBUTOR.value and (not project or project.assigned_admin_id != admin.id):
        return _page(request, "error.html", admin, auth, status_code=403, title="دسترسی مجاز نیست", message="این گفتگو در دسترس شما نیست.")
    target = f"/admin/conversations/{conversation.id}"
    try:
        document = await _save_reply_document(db, conversation, file, text)
    except UploadRejected:
        return redirect(target, "reply-file-rejected")
    await _queue_customer_message(db, conversation, admin, text, f"conversation-reply:{conversation.id}:{random_token(12)}", document=document)
    if project:
        db.add(ProjectEvent(project_id=project.id, actor_type="admin", actor_id=str(admin.id), event_type="reply", note=(text or (document.original_name if document else ""))[:1000], private=False))
    _audit(db, admin, "conversation.reply", "conversation", conversation.id, {})
    await _commit_reply(db, document)
    return redirect(f"/admin/conversations/{conversation.id}", "reply-queued")


@router.post("/conversations/{conversation_id}/delete-transcript")
async def delete_transcript(conversation_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db, "*", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    if admin.role != Role.OWNER.value:
        return _page(request, "error.html", admin, auth, status_code=403, title="دسترسی مجاز نیست", message="فقط مالک اصلی می‌تواند آرشیو را پاک کند.")
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        return redirect("/admin/conversations", "missing")
    count = int(await db.scalar(select(func.count(ConversationMessage.id)).where(
        ConversationMessage.conversation_id == conversation.id)) or 0)
    archived_files = list((await db.scalars(select(ConversationFile).where(
        ConversationFile.conversation_id == conversation.id))).all())
    archived_keys = [item.stored_key for item in archived_files]
    await db.execute(
        sql_update(OutboxMessage)
        .where(OutboxMessage.conversation_id == conversation.id)
        .values(status="canceled", body="", keyboard=None, provider_message_id=None,
                lease_until=None, transcript_message_id=None)
    )
    await db.execute(ConversationFile.__table__.delete().where(ConversationFile.conversation_id == conversation.id))
    await db.execute(ConversationMessage.__table__.delete().where(ConversationMessage.conversation_id == conversation.id))
    conversation.cleared_at = now_utc()
    conversation.state = "home"
    conversation.draft_data = {}
    conversation.ui_message_id = None
    _audit(db, admin, "conversation.transcript_deleted", "conversation", conversation.id, {"messages": count})
    await db.commit()
    upload_root = settings.uploads_dir.resolve()
    for key in archived_keys:
        still_project_file = await db.scalar(select(Attachment.id).where(Attachment.stored_key == key).limit(1))
        if not still_project_file and re.fullmatch(r"[a-f0-9]{32}", key):
            path = (upload_root / key).resolve()
            if path.parent == upload_root:
                path.unlink(missing_ok=True)
    return redirect(f"/admin/conversations/{conversation.id}", "transcript-deleted")


@router.get("/attachments/{attachment_id}")
async def download_attachment(attachment_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db, "projects.read")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    attachment = await db.get(Attachment, attachment_id)
    project = await db.get(Project, attachment.project_id) if attachment else None
    if not attachment or not project or (admin.role == Role.CONTRIBUTOR.value and project.assigned_admin_id != admin.id):
        return _page(request, "error.html", admin, auth, status_code=404, title="فایل پیدا نشد", message="فایل در دسترس نیست.")
    if not re.fullmatch(r"[a-f0-9]{32}", attachment.stored_key):
        return Response(status_code=404)
    root = settings.uploads_dir.resolve()
    path = (root / attachment.stored_key).resolve()
    if path.parent != root or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type=attachment.mime_type, filename=safe_filename(attachment.original_name),
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/conversation-files/{file_id}")
async def download_conversation_file(file_id: str, request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    archived = await db.get(ConversationFile, file_id)
    conversation = await db.get(Conversation, archived.conversation_id) if archived else None
    project = await db.scalar(select(Project).where(Project.conversation_id == conversation.id)) if conversation else None
    can_download = has_permission(admin.role, "customers.read") or (
        project is not None and (
            has_permission(admin.role, "projects.read") or (
                has_permission(admin.role, "projects.assigned.read") and project.assigned_admin_id == admin.id
            )
        )
    )
    if not archived or not conversation or not can_download:
        return _page(request, "error.html", admin, auth, status_code=404, title="فایل پیدا نشد", message="این فایل در دسترس نیست.")
    if not re.fullmatch(r"[a-f0-9]{32}", archived.stored_key):
        return Response(status_code=404)
    root = settings.uploads_dir.resolve()
    path = (root / archived.stored_key).resolve()
    if path.parent != root or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type=archived.mime_type, filename=safe_filename(archived.original_name),
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/team")
async def team_page(request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db, "team.invite")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    members = list((await db.scalars(select(AdminUser).order_by(AdminUser.created_at))).all())
    invites = list((await db.scalars(select(AdminInvite).where(AdminInvite.used_at.is_(None),
                                                             AdminInvite.expires_at > now_utc())
                                     .order_by(AdminInvite.created_at.desc()).limit(50))).all())
    allowed_roles = [Role.REVIEWER, Role.CONTRIBUTOR, Role.SUPPORT]
    if admin.role == Role.OWNER.value:
        allowed_roles.insert(0, Role.MANAGER)
    return _page(request, "team.html", admin, auth, members=members, invites=invites,
                 allowed_roles=allowed_roles, role_labels={role.value: role_label(role.value) for role in Role},
                 invite_url=request.query_params.get("invite_url", ""), bot_link=request.query_params.get("bot_link", ""),
                 telegram_bot_available=bool(settings.telegram_bot_username and settings.telegram_bot_token),
                 bale_bot_available=bool(settings.bale_bot_username and settings.bale_bot_token))


@router.post("/team/invites")
async def create_invite(request: Request, db: AsyncSession = Depends(get_session), role: str = Form("")):
    guard = await _guard(request, db, "team.invite", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    allowed = {Role.REVIEWER.value, Role.CONTRIBUTOR.value, Role.SUPPORT.value}
    if admin.role == Role.OWNER.value:
        allowed.add(Role.MANAGER.value)
    if role not in allowed:
        return redirect("/admin/team", "role-not-allowed")
    token = random_token(32)
    db.add(AdminInvite(token_digest=digest_token(token), role=role, created_by=admin.id, expires_at=expires_in(hours=48)))
    _audit(db, admin, "team.invite_created", "invite", "", {"role": role})
    await db.commit()
    # URL fragment tokens are never included in the HTTP request or access logs.
    url = f"{settings.public_base_url.rstrip('/')}/admin/join#{token}"
    return _page(request, "team.html", admin, auth, members=list((await db.scalars(select(AdminUser).order_by(AdminUser.created_at))).all()),
                 invites=[], allowed_roles=[Role(role)], role_labels={item.value: role_label(item.value) for item in Role},
                 invite_url=url, bot_link="", notice_text="دعوت یک‌بارمصرف تا ۴۸ ساعت معتبر است.",
                 telegram_bot_available=bool(settings.telegram_bot_username and settings.telegram_bot_token),
                 bale_bot_available=bool(settings.bale_bot_username and settings.bale_bot_token))


@router.post("/team/{target_id}/bot-link")
async def create_bot_link(target_id: int, request: Request, db: AsyncSession = Depends(get_session),
                          platform: str = Form("")):
    guard = await _guard(request, db, "team.invite", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    target = await db.get(AdminUser, target_id)
    if admin.role != Role.OWNER.value and admin.id != target_id:
        return _page(request, "error.html", admin, auth, status_code=403,
                     title="دسترسی مجاز نیست", message="مدیر پروژه فقط می‌تواند اتصال پیام‌رسان حساب خودش را بسازد.")
    username = settings.telegram_bot_username if platform == "telegram" else settings.bale_bot_username if platform == "bale" else ""
    if not target or not target.active or not username:
        return redirect("/admin/team", "bot-link-unavailable")
    token = random_token(24)
    db.add(BotLinkChallenge(token_digest=digest_token(token), admin_id=target.id, platform=platform,
                            expires_at=expires_in(hours=1)))
    _audit(db, admin, "team.bot_link_created", "admin", str(target.id), {"platform": platform})
    await db.commit()
    scheme = "https://t.me" if platform == "telegram" else "https://ble.ir"
    url = f"{scheme}/{username}?start=link_{token}"
    members = list((await db.scalars(select(AdminUser).order_by(AdminUser.created_at))).all())
    return _page(request, "team.html", admin, auth, members=members, invites=[], allowed_roles=[], role_labels={item.value: role_label(item.value) for item in Role}, invite_url="", bot_link=url,
                 notice_text="لینک اتصال یک‌بارمصرف است و یک ساعت اعتبار دارد.",
                 telegram_bot_available=bool(settings.telegram_bot_username and settings.telegram_bot_token),
                 bale_bot_available=bool(settings.bale_bot_username and settings.bale_bot_token))


@router.post("/team/{target_id}/deactivate")
async def deactivate_member(target_id: int, request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db, "*", post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    target = await db.get(AdminUser, target_id)
    if admin.role != Role.OWNER.value or not target or target.id == admin.id or target.role == Role.OWNER.value:
        return redirect("/admin/team", "cannot-deactivate")
    target.active = False
    target.session_version += 1
    _audit(db, admin, "team.deactivated", "admin", str(target.id), {})
    await db.commit()
    return redirect("/admin/team", "member-deactivated")


@router.get("/profile")
async def profile_page(request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    return _page(request, "profile.html", admin, auth)


@router.post("/profile/password")
async def profile_password(request: Request, db: AsyncSession = Depends(get_session),
                           current_password: str = Form(""), new_password: str = Form(""),
                           password_confirm: str = Form("")):
    guard = await _guard(request, db, post=True)
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    if not await asyncio.to_thread(verify_password, current_password, admin.password_hash) or new_password != password_confirm:
        return redirect("/admin/profile", "password-mismatch")
    try:
        admin.password_hash = await asyncio.to_thread(hash_password, new_password)
    except ValueError:
        return redirect("/admin/profile", "password-policy")
    admin.session_version += 1
    _audit(db, admin, "auth.password_changed", "admin", str(admin.id), {})
    await db.commit()
    return redirect("/admin/login", "password-updated")


@router.get("/audit")
async def audit_page(request: Request, db: AsyncSession = Depends(get_session)):
    guard = await _guard(request, db, "reports.read")
    if not isinstance(guard, tuple):
        return guard
    admin, auth = guard
    entries = list((await db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(300))).all())
    return _page(request, "audit.html", admin, auth, entries=entries)
