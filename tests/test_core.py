from __future__ import annotations

import asyncio
import re
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
async def test_admin_conversation_page_renders_and_csrf_is_enforced(db_context, monkeypatch) -> None:
    from pydantic import SecretStr

    from app.database import get_session
    from app.main import app

    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("test-token"))
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
                                             actor_label="مینا", provider_message_id="12", content="سلام",
                                             metadata_json={"support_session_id": "test"})
    session.add(ConversationMessage(conversation_id=conversation.id, direction="inbound", content="/start"))
    session.add_all([transcript_message, PlatformIdentity(platform="telegram", provider_user_id="88",
                                                          chat_id="88", username="minatest", customer_id=customer.id)])
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
            assert "مینا" in response.text and "@minatest" in response.text
            assert "سلام" not in response.text
            assert f"/admin/conversations/{conversation.id}" in (
                await client.get("/admin/conversations", params={"q": "@minatest"})
            ).text
            assert "1 جدید" in response.text
            assert "1 پیام و درخواست خوانده‌نشده" in (await client.get("/admin")).text
            assert (await client.get("/admin/login")).headers["location"] == "/admin"
            assert "/start" in (await client.get(f"/admin/conversations/{conversation.id}?logs=1&q=%2Fstart")).text
            assert f"/admin/conversations/{conversation.id}" in (await client.get("/admin/conversations?logs=1&q=%2Fstart")).text
            detail = await client.get(f"/admin/conversations/{conversation.id}")
            assert detail.status_code == 200 and "سلام" in detail.text and "@minatest" in detail.text
            assert "/start" not in detail.text
            assert "0 پیام و درخواست خوانده‌نشده" in (await client.get("/admin")).text
            stats = await client.get("/admin/stats?days=1")
            assert stats.status_code == 200 and "1 کاربر" in stats.text and "تقویم شمسی" in stats.text
            from app.calendar_fa import solar_date
            from app.stats import tehran_today

            day = solar_date(tehran_today())
            hourly = await client.get("/admin/stats", params={"day": day})
            assert hourly.status_code == 200 and "آمار ساعتی" in hourly.text and "23:00" in hourly.text
            chart = await client.get("/admin/stats/chart", params={"day": day})
            assert chart.status_code == 200 and chart.content.startswith(b"\x89PNG\r\n\x1a\n")

            assert response.headers["x-frame-options"] == "DENY"

            rejected = await client.post("/admin/team/invites", data={"role": "reviewer"})
            assert rejected.status_code == 403

            endpoint = f"/admin/conversations/{conversation.id}/reply"
            invalid = await client.post(
                endpoint, data={"csrf_token": session_csrf},
                files={"file": ("invalid.exe", b"data", "application/octet-stream")},
            )
            assert "notice=reply-file-rejected" in invalid.headers["location"]
            queued = await client.post(
                endpoint, data={"csrf_token": session_csrf},
                files={"file": ("delivery.pdf", b"%PDF-1.4 test", "application/pdf")},
            )
            assert "notice=reply-queued" in queued.headers["location"]
            async with maker() as verification:
                job = await verification.scalar(select(OutboxMessage).where(OutboxMessage.operation == "document"))
                archived_file = await verification.scalar(select(ConversationFile).where(
                    ConversationFile.message_id == job.transcript_message_id,
                ))
                assert job.chat_id == "88" and archived_file.original_name == "delivery.pdf"

            csrf_form = {"csrf_token": session_csrf}
            hidden = await client.post(f"/admin/conversations/{conversation.id}/hide", data=csrf_form)
            assert hidden.status_code == 303
            assert "@minatest" not in (await client.get("/admin/conversations")).text
            assert "@minatest" in (await client.get("/admin/conversations?archived=1")).text
            both = await client.post(f"/admin/conversations/{conversation.id}/remove-both", data=csrf_form)
            assert "notice=conversation-delete-queued" in both.headers["location"]
            async with maker() as verification:
                remove_job = await verification.scalar(select(OutboxMessage).where(OutboxMessage.operation == "delete"))
                assert remove_job.provider_message_id == "12"
                assert await verification.get(ConversationMessage, transcript_message.id)

            form = {**csrf_form, "body": "پیام نمونه", "audience": "telegram"}
            preview = await client.post("/admin/broadcasts", data=form)
            assert preview.status_code == 200 and "ارسال به 1 گفتگو" in preview.text
            async with maker() as verification:
                assert await verification.scalar(select(OutboxMessage).where(
                    OutboxMessage.dedupe_key.startswith("broadcast:"))) is None
            for field in ("batch", "confirmation"):
                form[field] = re.search(rf'name="{field}" value="([^"]+)"', preview.text).group(1)
            tampered = await client.post("/admin/broadcasts", data={**form, "body": "changed"})
            assert "notice=broadcast-audience-changed" in tampered.headers["location"]
            sent = await client.post("/admin/broadcasts", data=form)
            duplicate = await client.post("/admin/broadcasts", data=form)
            assert sent.headers["location"] == duplicate.headers["location"]
            assert (await client.get(sent.headers["location"])).status_code == 200
            async with maker() as verification:
                jobs = list(await verification.scalars(select(OutboxMessage).where(
                    OutboxMessage.dedupe_key.startswith("broadcast:"))))
                assert len(jobs) == 1 and jobs[0].chat_id == "88"

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
    password_hash = hash_password("Sample1!")
    assert verify_password("Sample1!", password_hash)
    assert not verify_password("incorrect-password", password_hash)
    for weak in ("Short1!", "sample1!", "SAMPLE1!", "Sample!!", "Sample12"):
        with pytest.raises(ValueError):
            hash_password(weak)
    assert digest_token("opaque-token") != digest_token("other-token")


@pytest.mark.asyncio
async def test_support_dashboard_shows_unread_without_project_access(db_context) -> None:
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
            assert home.status_code == 200
            assert "خوانده‌نشده" in home.text and "پروژه‌های تازه" not in home.text
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


@pytest.mark.asyncio
async def test_staff_delivery_requires_approval_and_reaches_customer(db_context, monkeypatch) -> None:
    from app.bot import worker
    from app.models import ProjectDelivery

    session, maker = db_context
    staff = AdminUser(username="sender", password_hash="unused", role=Role.CONTRIBUTOR.value)
    customer = Customer(display_name="Customer")
    owner = AdminUser(username="delivery-owner", password_hash="unused", role=Role.OWNER.value)
    session.add_all([staff, customer, owner])
    await session.flush()
    conversation = Conversation(platform="bale", chat_id="92", customer_id=customer.id)
    session.add_all([conversation, PlatformIdentity(
        platform="telegram", provider_user_id="91", chat_id="91", admin_id=staff.id,
    ), PlatformIdentity(platform="telegram", provider_user_id="93", chat_id="93", admin_id=owner.id),
       PlatformIdentity(platform="bale", provider_user_id="92", chat_id="92", customer_id=customer.id)])
    await session.flush()
    project = Project(public_id="CP-FILE", customer_id=customer.id, conversation_id=conversation.id,
                      title="IoT", description="Delivery test", status="in_progress")
    session.add(project)
    await session.flush()

    class FileAdapter(FakeAdapter):
        async def download_to(self, file_id, destination_dir, max_bytes):
            path = destination_dir / "incoming-delivery"
            raw = b"%PDF-1.4 test"
            path.write_bytes(raw)
            return path, "report.pdf", "application/pdf", len(raw)

        async def send_document(self, chat_id, path, filename, mime_type, caption):
            self.sent.append((chat_id, caption))
            assert path.read_bytes() == b"%PDF-1.4 test"
            assert filename == "report.pdf" and mime_type == "application/pdf"
            return "1234"

    adapter = FileAdapter()
    def document_update(update_id):
        update = _message_update(update_id, 91, "")
        update["message"].update(document={"file_id": "sample", "file_name": "report.pdf",
                                            "mime_type": "application/pdf"}, caption="تحویل پروژه")
        return update

    await handle_update(session, "telegram", _callback_update(201, 91, "a:file:CP-FILE"), adapter)
    await handle_update(session, "telegram", document_update(202), adapter)
    assert await session.scalar(select(OutboxMessage).where(OutboxMessage.operation == "document")) is None
    project.assigned_admin_id = staff.id
    await session.flush()
    await handle_update(session, "telegram", _callback_update(203, 91, "a:submit:CP-FILE"), adapter)
    await handle_update(session, "telegram", document_update(204), adapter)
    delivery = await session.scalar(select(ProjectDelivery))
    assert delivery.status == "draft"
    await handle_update(session, "telegram", _callback_update(205, 91, f"a:ds:{delivery.id}"), adapter)
    assert delivery.status == "pending"
    await handle_update(session, "bale", _callback_update(206, 92, f"c:files:{delivery.id}"), adapter)
    assert await session.scalar(select(OutboxMessage.id).where(OutboxMessage.operation == "final_file")) is None
    await handle_update(session, "telegram", _callback_update(207, 93, f"a:da:{delivery.id}"), adapter)
    assert project.status == "completed" and delivery.status == "approved"
    await handle_update(session, "bale", _callback_update(208, 92, f"c:files:{delivery.id}"), adapter)
    await handle_update(session, "bale", _callback_update(209, 92, f"c:files:{delivery.id}"), adapter)
    job = await session.scalar(select(OutboxMessage).where(OutboxMessage.operation == "final_file"))
    assert (job.platform, job.chat_id) == ("bale", "92")
    assert len(list(await session.scalars(select(OutboxMessage).where(OutboxMessage.operation == "final_file")))) == 1
    job.status = "processing"
    await session.commit()
    monkeypatch.setattr(worker, "SessionLocal", maker)
    assert await worker._dispatch_outbox(job.id, {"bale": adapter})
    assert adapter.sent == [("92", "فایل پروژهٔ CP-FILE")]
    session.expire_all()
    delivered = await session.scalar(select(ConversationMessage).where(ConversationMessage.provider_message_id == "1234"))
    assert delivered.metadata_json["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_staff_review_accepts_once_rejects_with_reason_and_dismisses_previews(db_context) -> None:
    from app.bot.flow import _queue_staff_notice

    db, _ = db_context
    adapter = FakeAdapter()
    owner = AdminUser(username="review-owner", password_hash="unused", role=Role.OWNER.value)
    customer = Customer(display_name="Requester")
    db.add_all([owner, customer])
    await db.flush()
    staff_identity = PlatformIdentity(platform="telegram", provider_user_id="810", chat_id="810", admin_id=owner.id)
    chat = Conversation(platform="telegram", chat_id="811", customer_id=customer.id, state="support",
                        draft_data={"support_session_id": "test"})
    db.add_all([staff_identity, chat, PlatformIdentity(
        platform="telegram", provider_user_id="811", chat_id="811", customer_id=customer.id,
    )])
    await db.flush()
    project = Project(public_id="CP-REVIEW", customer_id=customer.id, conversation_id=chat.id,
                      title="Preview title", description="Preview description")
    db.add(project)
    await db.flush()
    await _queue_staff_notice(db, staff_identity, owner, project)
    preview = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key.startswith("new-project:")))
    assert "Preview description" in preview.body
    preview.status = "sent"
    notice = await db.get(ConversationMessage, preview.transcript_message_id)
    notice.provider_message_id = "123"
    await handle_update(db, "telegram", _callback_update(962, 810, f"a:pa:{project.id}"), adapter)
    await handle_update(db, "telegram", _callback_update(963, 810, f"a:pa:{project.id}"), adapter)
    assert project.status == "reviewing"
    assert len(list(await db.scalars(select(OutboxMessage).where(OutboxMessage.operation == "review_del")))) == 1
    assert len(list(await db.scalars(select(OutboxMessage).where(OutboxMessage.dedupe_key.startswith("review-result:p:"))))) == 1

    await handle_update(db, "telegram", _message_update(964, 811, "Support question"), adapter)
    await db.flush()
    source = await db.scalar(select(ConversationMessage).where(ConversationMessage.content == "Support question",
                                                                ConversationMessage.direction == "inbound"))
    preview = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key.startswith("support-review:")))
    assert "Support question" in preview.body
    await handle_update(db, "telegram", _callback_update(965, 810, f"a:sr:{source.id}"), adapter)
    assert not source.metadata_json.get("support_decision")
    await handle_update(db, "telegram", _message_update(966, 810, "Please send your project number"), adapter)
    assert source.metadata_json["support_decision"] == "declined"
    assert preview.status == "canceled"
    reply = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key.startswith("review-result:s:")))
    assert reply.chat_id == "811" and "Please send your project number" in reply.body


@pytest.mark.asyncio
async def test_assignment_schedules_two_reminders_and_team_contact_can_reply(db_context) -> None:
    from app.bot.operations import cancel_reminders, schedule_reminders

    db, _ = db_context
    dev = AdminUser(username="delivery-dev", role=Role.CONTRIBUTOR.value, password_hash="unused")
    support = AdminUser(username="delivery-support", role=Role.SUPPORT.value, password_hash="unused")
    customer = Customer(display_name="Customer")
    db.add_all([dev, support, customer])
    await db.flush()
    client = Conversation(platform="telegram", chat_id="90", customer_id=customer.id)
    db.add_all([
        client,
        PlatformIdentity(platform="telegram", provider_user_id="850", chat_id="850", admin_id=dev.id),
        PlatformIdentity(platform="bale", provider_user_id="851", chat_id="851", admin_id=support.id),
    ])
    await db.flush()
    deadline = now_utc() + timedelta(days=5)
    project = Project(public_id="CP-DUE", customer_id=customer.id, conversation_id=client.id,
                      title="مهلت تحویل", description="Test deadline", work_due_at=deadline,
                      assigned_admin_id=dev.id, status="in_progress")
    db.add(project)
    await db.flush()
    await schedule_reminders(db, project, dev, "telegram")
    reminders = list(await db.scalars(select(OutboxMessage).where(
        OutboxMessage.dedupe_key.startswith(f"deadline:{project.id}:"),
    ).order_by(OutboxMessage.next_attempt_at)))
    assert [r.next_attempt_at.replace(tzinfo=deadline.tzinfo) for r in reminders] == [
        deadline - timedelta(days=2), deadline - timedelta(days=1),
    ]
    await cancel_reminders(db, project.id)
    assert all(r.status == "canceled" for r in reminders)
    adapter = FakeAdapter()
    await handle_update(db, "telegram", _callback_update(971, 850, f"a:peer:{support.id}"), adapter)
    await handle_update(db, "telegram", _message_update(972, 850, "Internal project question"), adapter)
    forwarded = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key == "team:telegram:972"))
    assert (forwarded.platform, forwarded.chat_id) == ("bale", "851")
    assert forwarded.keyboard[0][0]["callback_data"] == f"a:peer:{dev.id}"
