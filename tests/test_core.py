from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.bot import runtime as bot_runtime
from app.bot import worker as bot_worker
from app.bot.files import UploadRejected, register_download
from app.bot.flow import (
    _button,
    _home_keyboard,
    _parse_budget,
    _stored_bytes_for_conversation,
    handle_update,
)
from app.bot.transport import BotAdapter
from app.config import settings
from app.models import (
    AdminSession,
    AdminUser,
    Attachment,
    AuditEvent,
    Conversation,
    ConversationFile,
    ConversationMessage,
    Customer,
    OutboxMessage,
    PlatformIdentity,
    Project,
)
from app.permissions import Role, has_permission
from app.security import digest_token, hash_password, now_utc, random_token, verify_password


class FakeAdapter:
    platform = "telegram"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str, str]] = []
        self.sent_keyboards: list[list[list[dict[str, str]]] | None] = []
        self.edited_keyboards: list[list[list[dict[str, str]]] | None] = []

    async def send_message(self, chat_id: str, text: str, keyboard=None) -> str:
        self.sent.append((chat_id, text))
        self.sent_keyboards.append(keyboard)
        return str(500 + len(self.sent))

    async def edit_message_text(self, chat_id: str, message_id: str, text: str, keyboard=None) -> None:
        self.edited.append((chat_id, message_id, text))
        self.edited_keyboards.append(keyboard)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return True


def _message_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "first_name": "مینا"},
            "text": text,
        },
    }


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id, "first_name": "مینا"},
            "data": data,
            "message": {
                "message_id": update_id,
                "chat": {"id": user_id, "type": "private"},
            },
        },
    }


@pytest.mark.asyncio
async def test_project_intake_is_private_and_archived(db_context) -> None:
    session, _ = db_context
    adapter = FakeAdapter()
    user_id = 1234

    group_update = _message_update(1, user_id, "/start")
    group_update["message"]["chat"]["type"] = "group"
    await handle_update(session, "telegram", group_update, adapter)
    assert await session.scalar(select(Conversation.id)) is None

    await handle_update(session, "telegram", _message_update(2, user_id, "/start"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(3, user_id, "c:new"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(4, user_id, "c:track:computer"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _message_update(5, user_id, "طراحی وب‌سایت دانشگاهی"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _message_update(6, user_id, "یک وب‌سایت واکنش‌گرا با پنل مدیریت و مستندات می‌خواهم."), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(7, user_id, "c:skip_file"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(8, user_id, "c:skip_deadline"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(9, user_id, "c:skip_budget"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(10, user_id, "c:confirm"), adapter)
    await session.commit()

    project = await session.scalar(select(Project))
    transcript = list((await session.scalars(select(ConversationMessage))).all())
    assert project is not None
    assert project.title == "طراحی وب‌سایت دانشگاهی"
    assert project.subject_area == "computer"
    assert len(transcript) >= 8
    assert any("توضیح" in item.content for item in transcript)
    assert await session.scalar(select(Customer.id)) is not None


@pytest.mark.asyncio
async def test_support_clear_only_queues_current_session_and_preserves_archive(db_context) -> None:
    session, _ = db_context
    customer = Customer(display_name="مینا")
    session.add(customer)
    await session.flush()
    identity = PlatformIdentity(platform="telegram", provider_user_id="22", chat_id="22",
                                customer_id=customer.id)
    conversation = Conversation(platform="telegram", chat_id="22", customer_id=customer.id,
                                state="clear_confirm", draft_data={"support_session_id": "active"})
    session.add_all([identity, conversation])
    await session.flush()
    session.add_all([
        ConversationMessage(conversation_id=conversation.id, provider_message_id="101", revision=1,
                            direction="inbound", actor_label="مینا", content="پیام جاری",
                            metadata_json={"support_session_id": "active"}),
        ConversationMessage(conversation_id=conversation.id, provider_message_id="102", revision=1,
                            direction="outbound", actor_label="تیم", content="پاسخ جاری",
                            metadata_json={"support_session_id": "active"}),
        ConversationMessage(conversation_id=conversation.id, provider_message_id="103", revision=1,
                            direction="outbound", actor_label="پیام راهنمای تیم", content="کنترل فعلی",
                            metadata_json={"support_session_id": "active"}),
        ConversationMessage(conversation_id=conversation.id, provider_message_id="201", revision=1,
                            direction="inbound", actor_label="مینا", content="پیام قدیمی",
                            metadata_json={"support_session_id": "previous"}),
    ])
    await session.commit()

    await handle_update(session, "telegram", _callback_update(100, 22, "c:clear_yes"), FakeAdapter())
    await session.commit()
    delete_jobs = list((await session.scalars(select(OutboxMessage).where(
        OutboxMessage.operation == "delete"))).all())
    assert {item.provider_message_id for item in delete_jobs} == {"101", "102"}
    assert await session.scalar(select(ConversationMessage.id).limit(1)) is not None
    assert conversation.cleared_at is not None
    assert conversation.state == "support"


@pytest.mark.asyncio
async def test_start_menu_and_registration_use_compact_inline_edit_flow(db_context) -> None:
    session, _ = db_context
    adapter = FakeAdapter()
    await handle_update(session, "telegram", _message_update(1, 78, "/start"), adapter)
    await session.commit()
    conversation = await session.scalar(select(Conversation))
    assert conversation is not None
    start = await session.scalar(select(OutboxMessage).order_by(OutboxMessage.id.desc()))
    assert start.body == "سلام مینا 👋 از کجا شروع کنیم؟"
    assert len(start.keyboard) == 2
    assert [button["text"] for row in start.keyboard for button in row] == ["ثبت پروژه", "پیگیری", "پشتیبانی"]
    assert all(button.get("callback_data") != "c:clear" for row in start.keyboard for button in row)
    assert start.keyboard[0][0]["style"] == "primary"

    conversation.ui_message_id = "501"
    await handle_update(session, "telegram", _callback_update(2, 78, "c:new"), adapter)
    await session.commit()
    selection = await session.scalar(select(OutboxMessage).order_by(OutboxMessage.id.desc()))
    assert selection.operation == "edit"
    assert selection.body == "رشته‌ات رو انتخاب کن"
    assert selection.keyboard == [[{"text": "کامپیوتر", "callback_data": "c:track:computer", "style": "primary"}]]


@pytest.mark.asyncio
async def test_support_reopens_with_archived_history_and_tags_new_messages(db_context) -> None:
    from app.web.admin import _queue_customer_message

    session, _ = db_context
    customer = Customer(display_name="مینا")
    session.add(customer)
    await session.flush()
    identity = PlatformIdentity(platform="telegram", provider_user_id="79", chat_id="79",
                                customer_id=customer.id)
    conversation = Conversation(platform="telegram", chat_id="79", customer_id=customer.id,
                                state="home", draft_data={})
    session.add_all([identity, conversation])
    await session.flush()
    session.add_all([
        ConversationMessage(conversation_id=conversation.id, direction="inbound", actor_label="مینا",
                            content="پیام قبلی", metadata_json={"support_session_id": "old"}),
        ConversationMessage(conversation_id=conversation.id, direction="outbound", actor_label="تیم",
                            content="پاسخ قبلی", metadata_json={"support_session_id": "old"}),
    ])
    await session.commit()

    await handle_update(session, "telegram", _callback_update(1, 79, "c:help"), FakeAdapter())
    await session.commit()
    prompt = await session.scalar(select(OutboxMessage).order_by(OutboxMessage.id.desc()))
    assert "پیام قبلی" in prompt.body and "پاسخ قبلی" in prompt.body
    active_id = conversation.draft_data["support_session_id"]

    await handle_update(session, "telegram", _message_update(2, 79, "پیام تازه برای تیم"), FakeAdapter())
    await session.commit()
    inbound = await session.scalar(select(ConversationMessage).where(
        ConversationMessage.content == "پیام تازه برای تیم"))
    assert inbound.metadata_json["support_session_id"] == active_id

    admin = AdminUser(username="support-reply", password_hash="not-used", role=Role.SUPPORT.value,
                      display_name="تیم پشتیبانی")
    session.add(admin)
    await session.flush()
    await _queue_customer_message(session, conversation, admin, "پاسخ تازه", "support-reply-test")
    await session.commit()
    reply = await session.scalar(select(ConversationMessage).where(ConversationMessage.content == "پاسخ تازه"))
    assert reply.metadata_json["support_session_id"] == active_id


@pytest.mark.asyncio
async def test_customer_cannot_open_staff_callbacks_or_see_staff_menu(db_context) -> None:
    session, _ = db_context
    adapter = FakeAdapter()
    await handle_update(session, "telegram", _message_update(1, 80, "/start"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(2, 80, "a:queue"), adapter)
    await session.commit()
    response = await session.scalar(select(OutboxMessage).order_by(OutboxMessage.id.desc()))
    assert response.body == "این گزینه در دسترس نیست یا دسترسی لازم را نداری."
    assert all(button.get("callback_data") != "a:queue" for row in _home_keyboard() for button in row)


@pytest.mark.asyncio
async def test_customer_tracking_does_not_disclose_another_customers_project(db_context) -> None:
    session, _ = db_context
    own = Customer(display_name="مینا")
    other = Customer(display_name="شخص دیگر")
    session.add_all([own, other])
    await session.flush()
    session.add(PlatformIdentity(platform="telegram", provider_user_id="81", chat_id="81",
                                 customer_id=own.id))
    conversation = Conversation(platform="telegram", chat_id="81", customer_id=own.id,
                                state="home", draft_data={})
    session.add(conversation)
    await session.flush()
    project = Project(public_id="CP-PRIVATE", customer_id=other.id, conversation_id=conversation.id,
                      title="عنوان خصوصی", description="این پروژه متعلق به کاربر دیگری است.")
    session.add(project)
    await session.commit()

    await handle_update(session, "telegram", _callback_update(1, 81, "c:projects"), FakeAdapter())
    await session.commit()
    no_projects = await session.scalar(select(OutboxMessage).order_by(OutboxMessage.id.desc()))
    assert no_projects.body == "درخواستی برای پیگیری پیدا نشد."
    await handle_update(session, "telegram", _callback_update(2, 81, "c:project:CP-PRIVATE"), FakeAdapter())
    await session.commit()
    denied = await session.scalar(select(OutboxMessage).order_by(OutboxMessage.id.desc()))
    assert "در دسترس نیست" in denied.body
    assert "عنوان خصوصی" not in denied.body


@pytest.mark.asyncio
async def test_inline_button_styles_are_supported_by_telegram_and_bale() -> None:
    keyboard = [[_button("تأیید", "c:confirm", style="success"),
                 _button("لغو", "c:cancel", style="danger")]]
    telegram = object.__new__(BotAdapter)
    telegram.platform = "telegram"
    bale = object.__new__(BotAdapter)
    bale.platform = "bale"
    assert telegram._keyboard(keyboard) == keyboard
    assert bale._keyboard(keyboard) == keyboard


@pytest.mark.asyncio
async def test_poller_waits_for_expired_lease_instead_of_exiting(monkeypatch) -> None:
    class PollerAdapter:
        platform = "telegram"

        def __init__(self) -> None:
            self.closed = False

        async def get_updates(self, offset, timeout):
            raise AssertionError("lease must be renewed before polling")

        async def close(self) -> None:
            self.closed = True

    adapter = PollerAdapter()
    claim_results = iter((False, True))
    sleep_calls: list[float] = []

    async def claim(platform: str, owner: str) -> bool:
        return next(claim_results)

    async def no_lease(platform: str, owner: str) -> bool:
        return False

    async def no_offset(platform: str) -> int | None:
        return None

    async def release(platform: str, owner: str) -> None:
        return None

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if seconds != bot_runtime.LEASE_RETRY_SECONDS:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot_runtime, "configured_adapters", lambda: {"telegram": adapter})
    monkeypatch.setattr(bot_runtime, "_claim_lease", claim)
    monkeypatch.setattr(bot_runtime, "_renew_lease", no_lease)
    monkeypatch.setattr(bot_runtime, "_last_offset", no_offset)
    monkeypatch.setattr(bot_runtime, "_release_lease", release)
    monkeypatch.setattr(bot_runtime.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await bot_runtime.run_bot("telegram")

    assert sleep_calls == [bot_runtime.LEASE_RETRY_SECONDS, 1]
    assert adapter.closed


@pytest.mark.asyncio
async def test_worker_waits_for_previous_lease_before_processing(monkeypatch) -> None:
    lease_attempts = iter((False, True))
    events: list[str] = []

    async def lease(owner: str, *, release: bool = False) -> bool:
        events.append("release" if release else "claim")
        return True if release else next(lease_attempts)

    async def wait(seconds: float) -> None:
        assert seconds == bot_worker.WORKER_LEASE_RETRY_SECONDS
        events.append("wait")

    async def process_incoming(adapters) -> int:
        events.append("process")
        raise asyncio.CancelledError

    monkeypatch.setattr(bot_worker, "configured_adapters", dict)
    monkeypatch.setattr(bot_worker, "_worker_lease", lease)
    monkeypatch.setattr(bot_worker.asyncio, "sleep", wait)
    monkeypatch.setattr(bot_worker, "process_incoming_batch", process_incoming)

    with pytest.raises(asyncio.CancelledError):
        await bot_worker.run_worker()

    assert events == ["claim", "wait", "claim", "process", "release"]


@pytest.mark.asyncio
async def test_admin_login_redirect_health_and_theme_assets() -> None:
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        admin = await client.get("/admin", follow_redirects=False)
        assert admin.status_code == 303
        assert admin.headers["location"].startswith("/admin/login?notice=sign-in")
        login = await client.get("/admin/login")
        assert login.status_code == 200
        assert 'dir="rtl"' in login.text
        assert 'data-theme="dark"' in login.text
        assert "data-theme-toggle" in login.text
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"

    app_root = Path(__file__).parents[1]
    css = (app_root / "app/static/admin.css").read_text()
    js = (app_root / "app/static/admin.js").read_text()
    assert 'html[data-theme="light"]' in css and "#080b14" in css
    assert 'localStorage.setItem("chitoproject-theme"' in js
    assert 'localStorage.getItem("chitoproject-theme")' in js


@pytest.mark.asyncio
async def test_archive_quota_counts_project_files_without_double_counting(db_context) -> None:
    session, _ = db_context
    customer = Customer(display_name="مینا")
    session.add(customer)
    await session.flush()
    conversation = Conversation(platform="telegram", chat_id="23", customer_id=customer.id,
                                state="home", draft_data={})
    session.add(conversation)
    await session.flush()
    project = Project(public_id="CP-QUOTA", customer_id=customer.id, conversation_id=conversation.id,
                      title="پروژهٔ نمونه", description="توضیح کافی برای پروژهٔ نمونه")
    session.add(project)
    await session.flush()
    message = ConversationMessage(conversation_id=conversation.id, direction="inbound", content="فایل")
    session.add(message)
    await session.flush()
    session.add(ConversationFile(conversation_id=conversation.id, message_id=message.id,
                                 stored_key="a" * 32, original_name="a.pdf", mime_type="application/pdf",
                                 sha256="a" * 64, size_bytes=15 * 1024 * 1024))
    session.add_all([
        Attachment(project_id=project.id, stored_key="a" * 32, original_name="a.pdf",
                   mime_type="application/pdf", sha256="a" * 64, size_bytes=15 * 1024 * 1024),
        Attachment(project_id=project.id, stored_key="b" * 32, original_name="b.pdf",
                   mime_type="application/pdf", sha256="b" * 64, size_bytes=10 * 1024 * 1024),
    ])
    await session.commit()

    assert await _stored_bytes_for_conversation(session, conversation.id) == 25 * 1024 * 1024
    await session.delete(await session.get(ConversationFile, (await session.scalar(select(ConversationFile.id)))))
    await session.flush()
    assert await _stored_bytes_for_conversation(session, conversation.id) == 25 * 1024 * 1024


@pytest.mark.asyncio
async def test_ui_queue_edits_latest_control_message(db_context, monkeypatch) -> None:
    session, maker = db_context
    conversation = Conversation(platform="telegram", chat_id="44", state="home", draft_data={})
    session.add(conversation)
    await session.flush()
    first = ConversationMessage(conversation_id=conversation.id, direction="outbound", content="اول")
    second = ConversationMessage(conversation_id=conversation.id, direction="outbound", content="دوم")
    session.add_all([first, second])
    await session.flush()
    first_job = OutboxMessage(dedupe_key="ui-first", platform="telegram", chat_id="44",
                              conversation_id=conversation.id, transcript_message_id=first.id,
                              operation="send", body="اول", use_as_ui=True, force_send=True,
                              status="processing")
    second_job = OutboxMessage(dedupe_key="ui-second", platform="telegram", chat_id="44",
                               conversation_id=conversation.id, transcript_message_id=second.id,
                               operation="send", body="دوم", use_as_ui=True, status="processing")
    session.add_all([first_job, second_job])
    await session.commit()

    from app.bot import worker

    monkeypatch.setattr(worker, "SessionLocal", maker)
    fake = FakeAdapter()
    assert await worker._dispatch_outbox(first_job.id, {"telegram": fake})
    assert await worker._dispatch_outbox(second_job.id, {"telegram": fake})
    assert len(fake.sent) == 1
    assert fake.edited == [("44", "501", "دوم")]


@pytest.mark.asyncio
async def test_admin_conversation_page_renders_and_csrf_is_enforced(db_context) -> None:
    from app.database import get_session
    from app.main import app

    session, maker = db_context
    admin = AdminUser(username="owner-test", password_hash="not-used-in-this-test",
                      role=Role.OWNER.value, display_name="مدیر")
    session.add(admin)
    await session.flush()
    raw_token = random_token(32)
    session_csrf = random_token(24)
    session.add(AdminSession(token_digest=digest_token(raw_token), admin_id=admin.id,
                             session_version=admin.session_version, csrf_token=session_csrf,
                             expires_at=now_utc() + timedelta(hours=1)))
    customer = Customer(display_name="مینا")
    session.add(customer)
    await session.flush()
    conversation = Conversation(platform="telegram", chat_id="88", customer_id=customer.id,
                                state="home", draft_data={})
    session.add(conversation)
    await session.flush()
    transcript_message = ConversationMessage(conversation_id=conversation.id, direction="inbound",
                                             actor_label="مینا", content="سلام")
    session.add(transcript_message)
    await session.flush()
    stored_key = "a" * 32
    (settings.uploads_dir / stored_key).write_bytes(b"private-file")
    session.add(ConversationFile(conversation_id=conversation.id, message_id=transcript_message.id,
                                 stored_key=stored_key, original_name="sample.pdf", mime_type="application/pdf",
                                 sha256="b" * 64, size_bytes=12))
    session.add(OutboxMessage(dedupe_key="outgoing-secret", platform="telegram", chat_id="88",
                              conversation_id=conversation.id, transcript_message_id=transcript_message.id,
                              operation="send", body="پاسخ محرمانه", status="sent"))
    await session.commit()

    async def override_session():
        async with maker() as request_session:
            yield request_session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            client.cookies.set("chitoproject_session", raw_token, path="/admin")
            response = await client.get("/admin/conversations")
            assert response.status_code == 200
            assert "گفتگوهای مشتریان" in response.text
            assert "سلام" in response.text
            assert response.headers["x-frame-options"] == "DENY"

            rejected = await client.post("/admin/team/invites", data={"role": "reviewer"})
            assert rejected.status_code == 403

            deleted = await client.post(
                f"/admin/conversations/{conversation.id}/delete-transcript",
                data={"csrf_token": session_csrf},
            )
            assert deleted.status_code == 303
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert await session.scalar(select(ConversationMessage.id)) is None
    archived_job = await session.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key == "outgoing-secret"))
    assert archived_job is not None and archived_job.status == "canceled" and archived_job.body == ""
    assert not (settings.uploads_dir / stored_key).exists()
    assert await session.scalar(select(AuditEvent.id).where(AuditEvent.action == "conversation.transcript_deleted")) is not None


def test_budget_parser_and_callback_limit() -> None:
    assert _parse_budget("۱۲۳٬۴۵۶") == 123456
    assert _parse_budget("   ") is None
    with pytest.raises(ValueError):
        _parse_budget("۱۲۳ تومان")
    with pytest.raises(ValueError):
        _button("گزینه", "x" * 65)


def test_roles_and_password_hashing() -> None:
    assert has_permission(Role.OWNER, "customers.read")
    assert has_permission(Role.CONTRIBUTOR, "projects.assigned.read")
    assert not has_permission(Role.CONTRIBUTOR, "projects.read")
    assert has_permission(Role.SUPPORT, "customers.read")
    assert has_permission(Role.SUPPORT, "messages.reply")
    assert not has_permission(Role.SUPPORT, "projects.read")
    assert not has_permission("unknown", "projects.read")
    password_hash = hash_password("very-long-test-password")
    assert verify_password("very-long-test-password", password_hash)
    assert not verify_password("incorrect-password", password_hash)
    assert digest_token("opaque-token") != digest_token("other-token")


@pytest.mark.asyncio
async def test_support_lands_in_conversations_without_project_access(db_context) -> None:
    from app.database import get_session
    from app.main import app

    session, maker = db_context
    support = AdminUser(username="support-test", password_hash="not-used-in-this-test",
                        role=Role.SUPPORT.value, display_name="پشتیبانی")
    session.add(support)
    await session.flush()
    raw_token = random_token(32)
    session.add(AdminSession(token_digest=digest_token(raw_token), admin_id=support.id,
                             session_version=support.session_version, csrf_token=random_token(24),
                             expires_at=now_utc() + timedelta(hours=1)))
    customer = Customer(display_name="مینا")
    session.add(customer)
    await session.flush()
    conversation = Conversation(platform="telegram", chat_id="900", customer_id=customer.id,
                                state="home", draft_data={})
    session.add(conversation)
    await session.flush()
    session.add(ConversationMessage(conversation_id=conversation.id, direction="inbound",
                                    actor_label="مینا", content="پیام پشتیبانی"))
    await session.commit()

    async def override_session():
        async with maker() as request_session:
            yield request_session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            client.cookies.set("chitoproject_session", raw_token, path="/admin")
            home = await client.get("/admin", follow_redirects=False)
            assert home.status_code == 303
            assert home.headers["location"] == "/admin/conversations"
            inbox = await client.get("/admin/conversations")
            assert inbox.status_code == 200
            assert "مینا" in inbox.text
            projects = await client.get("/admin/projects")
            assert projects.status_code == 403
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_manager_cannot_create_bot_link_for_another_member(db_context, monkeypatch) -> None:
    from pydantic import SecretStr

    from app.database import get_session
    from app.main import app

    session, maker = db_context
    manager = AdminUser(username="manager-test", password_hash="not-used-in-this-test",
                        role=Role.MANAGER.value, display_name="مدیر")
    other = AdminUser(username="reviewer-test", password_hash="not-used-in-this-test",
                      role=Role.REVIEWER.value, display_name="بازبین")
    session.add_all([manager, other])
    await session.flush()
    csrf = random_token(24)
    raw_token = random_token(32)
    session.add(AdminSession(token_digest=digest_token(raw_token), admin_id=manager.id,
                             session_version=manager.session_version, csrf_token=csrf,
                             expires_at=now_utc() + timedelta(hours=1)))
    await session.commit()
    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("test-token"))
    monkeypatch.setattr(settings, "telegram_bot_username", "test_bot")

    async def override_session():
        async with maker() as request_session:
            yield request_session

    app.dependency_overrides[get_session] = override_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            client.cookies.set("chitoproject_session", raw_token, path="/admin")
            response = await client.post(f"/admin/team/{other.id}/bot-link",
                                         data={"platform": "telegram", "csrf_token": csrf})
            assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert await session.scalar(select(OutboxMessage.id)) is None
    from app.models import BotLinkChallenge

    assert await session.scalar(select(BotLinkChallenge.id)) is None


def test_public_csrf_double_submit_is_signed() -> None:
    from starlette.requests import Request

    from app.web.common import FORM_CSRF_COOKIE, new_form_csrf, valid_double_submit

    token = random_token(24)
    signature = digest_token("csrf:" + token)

    def request(cookie: str) -> Request:
        return Request({
            "type": "http", "method": "GET", "path": "/admin/join", "query_string": b"",
            "headers": [(b"cookie", f"{FORM_CSRF_COOKIE}={cookie}".encode())],
            "server": ("testserver", 80), "client": ("127.0.0.1", 1), "scheme": "http",
        })

    signed = request(f"{token}.{signature}")
    assert new_form_csrf(signed) == token
    assert valid_double_submit(signed, token)
    tampered = request(f"{token}.{'0' * 64}")
    assert new_form_csrf(tampered) != token
    assert not valid_double_submit(tampered, token)


def test_upload_validation_and_private_random_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "uploads_dir", tmp_path)
    png = tmp_path / "tmp-image"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    stored = register_download(png, "../../sample.png", "image/png")
    assert stored.path.parent == tmp_path
    assert stored.original_name == "sample.png"
    assert len(stored.stored_key) == 32
    assert stored.path.exists()

    bad = tmp_path / "bad"
    bad.write_bytes(b"executable-data")
    with pytest.raises(UploadRejected):
        register_download(bad, "bad.exe", "application/octet-stream")
    assert not bad.exists()
