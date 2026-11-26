from itertools import count
from types import SimpleNamespace

import pytest
import pyzipper
from pydantic import SecretStr
from sqlalchemy import select

from app.bot.flow import _stage_action, _stage_input
from app.bot.payment import (
    archive_password,
    create_encrypted_archive,
    stage_configured,
    stage_job,
    stage_payload,
)
from app.bot.transport import ProviderError
from app.bot.worker import _send_stage_job
from app.config import settings
from app.models import (
    Conversation,
    Customer,
    OutboxMessage,
    PaymentStage,
    Project,
    ProjectDelivery,
    ProjectDeliveryFile,
    now_utc,
)


@pytest.mark.asyncio
async def test_payment_stage_is_versioned_and_releases_zip_before_password(db_context, monkeypatch):
    db, _ = db_context
    monkeypatch.setattr(settings, "manual_payment_card", SecretStr("0" * 16))
    monkeypatch.setattr(settings, "payment_archive_key", SecretStr("test archive key".ljust(40, "!")))

    customer = Customer(display_name="مینا")
    db.add(customer)
    await db.flush()
    chat = Conversation(platform="bale", chat_id="42", customer_id=customer.id, state="home", draft_data={})
    db.add(chat)
    await db.flush()
    project = Project(public_id="CP-TEST", title="تست", description="شرح", status="in_progress",
                      customer_id=customer.id, conversation_id=chat.id, staged_payment=True)
    db.add(project)
    await db.flush()
    delivery = ProjectDelivery(project_id=project.id, status="stage_reviewed")
    db.add(delivery)
    await db.flush()
    source = settings.uploads_dir / ("a" * 32)
    contents = b"test deliverable"
    source.write_bytes(contents)
    file = ProjectDeliveryFile(delivery_id=delivery.id, stored_key=source.name, original_name="source.txt",
                               mime_type="text/plain", size_bytes=len(contents))
    stage = PaymentStage(project_id=project.id, delivery_id=delivery.id, number=1, stage_count=2, title="نمونه",
                         amount_toman=50000, preview_key="b" * 32, archive_key="c" * 32)
    assert stage_configured("bale", stage)
    assert not stage_configured("telegram", stage)
    db.add_all([file, stage])
    await db.flush()

    update_ids = count(1)
    adapter = SimpleNamespace()
    for version in (1, 2):
        stage.version = version
        stage.status = "preview_pending"
        await _stage_action(db, chat, "pr", stage.id, version, {"update_id": next(update_ids)})
        assert stage.revision_requests == version - 1
        await _stage_input(db, adapter, chat, {"text": "کوتاه"}, {"update_id": next(update_ids)})
        assert stage.revision_requests == version - 1
        await _stage_input(db, adapter, chat, {"text": "رنگ و اندازهٔ متن در صفحهٔ اصلی اصلاح شود."}, {"update_id": next(update_ids)})
        assert stage.revision_requests == version
        assert stage.status == "changes_requested"

    stage.version, stage.status = 3, "preview_pending"
    await _stage_action(db, chat, "pr", stage.id, 3, {"update_id": next(update_ids)})
    assert chat.state == "home"
    await _stage_action(db, chat, "pa", stage.id, 2, {"update_id": next(update_ids)})
    await _stage_action(db, chat, "pa", stage.id, 3, {"update_id": next(update_ids)})
    assert stage.status == "preview_pending"
    await stage_job(db, chat, stage, "stage_video")
    video = await db.scalar(select(OutboxMessage).where(
        OutboxMessage.dedupe_key == f"stage_video:{stage_payload(stage)}"))
    video.status = "sent"
    await _stage_action(db, chat, "pa", stage.id, 3, {"update_id": next(update_ids)})
    assert stage.status == "awaiting_payment"

    calls = []

    async def record(*args):
        calls.append(args)

    adapter.send_message = adapter.send_document = record
    payload = stage_payload(stage)
    await _send_stage_job(db, adapter, SimpleNamespace(operation="stage_card", chat_id=chat.chat_id, body=payload), chat)
    assert 21590 < (stage.deadline - now_utc()).total_seconds() <= 21600
    assert calls[0][-1][0][0]["copy_text"]["text"] == "0" * 16

    stage.archive_key = create_encrypted_archive(stage.id, stage.version, [file])
    archive_path = settings.uploads_dir / stage.archive_key
    password = archive_password(stage.id, stage.version)
    with pyzipper.AESZipFile(archive_path) as archive:
        with pytest.raises(RuntimeError):
            archive.read("01-source.txt")
        archive.setpassword(password.encode())
        assert archive.read("01-source.txt") == contents

    await stage_job(db, chat, stage, "stage_zip")
    archive_job = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key == f"stage_zip:{payload}"))
    with pytest.raises(ProviderError):
        await _send_stage_job(db, adapter, archive_job, chat)
    stage.status, stage.verified_at = "paid", now_utc()
    await _send_stage_job(db, adapter, archive_job, chat)
    secret_job = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key == f"stage_secret:{payload}"))
    with pytest.raises(ProviderError):
        await _send_stage_job(db, adapter, secret_job, chat)
    archive_job.status = "sent"
    await _send_stage_job(db, adapter, secret_job, chat)
    assert calls[1][1] == archive_path
    assert password in calls[2][1]
    assert password not in secret_job.body
