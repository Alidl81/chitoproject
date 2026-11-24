from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.bot.files import UploadRejected, register_download
from app.bot.flow import _button, _parse_budget, _stored_bytes_for_conversation, handle_update
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

    async def send_message(self, chat_id: str, text: str, keyboard=None) -> str:
        self.sent.append((chat_id, text))
        return str(500 + len(self.sent))

    async def edit_message_text(self, chat_id: str, message_id: str, text: str, keyboard=None) -> None:
        self.edited.append((chat_id, message_id, text))

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
    await handle_update(session, "telegram", _message_update(4, user_id, "طراحی وب‌سایت دانشگاهی"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _message_update(5, user_id, "یک وب‌سایت واکنش‌گرا با پنل مدیریت و مستندات می‌خواهم."), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(6, user_id, "c:skip_file"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(7, user_id, "c:skip_deadline"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(8, user_id, "c:skip_budget"), adapter)
    await session.commit()
    await handle_update(session, "telegram", _callback_update(9, user_id, "c:confirm"), adapter)
    await session.commit()

    project = await session.scalar(select(Project))
    transcript = list((await session.scalars(select(ConversationMessage))).all())
    assert project is not None
    assert project.title == "طراحی وب‌سایت دانشگاهی"
    assert len(transcript) >= 8
    assert any("توضیح" in item.content for item in transcript)
    assert await session.scalar(select(Customer.id)) is not None


@pytest.mark.asyncio
async def test_clear_chat_queues_all_message_ids_without_deleting_archive(db_context) -> None:
    session, _ = db_context
    customer = Customer(display_name="مینا")
    session.add(customer)
    await session.flush()
    identity = PlatformIdentity(platform="telegram", provider_user_id="22", chat_id="22",
                                customer_id=customer.id)
    conversation = Conversation(platform="telegram", chat_id="22", customer_id=customer.id,
                                state="home", draft_data={})
    session.add_all([identity, conversation])
    await session.flush()
    session.add_all([
        ConversationMessage(conversation_id=conversation.id, provider_message_id=str(i), revision=1,
                            direction="inbound", actor_label="مینا", content=f"پیام {i}")
        for i in range(1, 61)
    ])
    await session.commit()

    await handle_update(session, "telegram", _callback_update(100, 22, "c:clear_yes"), FakeAdapter())
    await session.commit()
    delete_jobs = list((await session.scalars(select(OutboxMessage).where(
        OutboxMessage.operation == "delete"))).all())
    assert len(delete_jobs) == 60
    assert await session.scalar(select(ConversationMessage.id).limit(1)) is not None
    assert conversation.cleared_at is not None


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
    project = Project(public_id="PH-QUOTA", customer_id=customer.id, conversation_id=conversation.id,
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
            client.cookies.set("projecthub_session", raw_token, path="/admin")
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
            client.cookies.set("projecthub_session", raw_token, path="/admin")
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
            client.cookies.set("projecthub_session", raw_token, path="/admin")
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
