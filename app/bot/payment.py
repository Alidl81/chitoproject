"""Manual, versioned delivery stages used by the Bale checkout flow."""

import hashlib
import hmac
import re
import shutil
from datetime import UTC, timedelta
from pathlib import Path
from uuid import uuid4

import pyzipper
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.files import safe_filename
from app.checkout import website_enabled
from app.config import settings
from app.models import (
    Conversation,
    OutboxMessage,
    PaymentStage,
    Project,
    ProjectDeliveryFile,
    now_utc,
)

PAYMENT_WINDOW = timedelta(hours=6)
MAX_PACKAGE_BYTES = 45_000_000
REVISIONS_PER_STAGE = 2


def card_number() -> str | None:
    setting = settings.manual_payment_card
    value = re.sub(r"[ -]", "", setting.get_secret_value().strip()) if setting else ""
    return value if re.fullmatch(r"[0-9]{16}", value) else None


def configured_for(platform: str) -> bool:
    key = settings.payment_archive_key
    return platform in {"telegram", "bale"} and website_enabled() and bool(
        key and len(key.get_secret_value().encode()) >= 32
    )


def stage_configured(platform: str, stage: PaymentStage) -> bool:
    return configured_for(platform) if stage.website_checkout else platform == "bale" and bool(card_number())


def asset_path(key: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        raise ValueError("invalid asset")
    root = settings.uploads_dir.resolve()
    path = root / key
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing asset")
    return path


def archive_password(stage_id: str, version: int) -> str:
    key = settings.payment_archive_key
    if not key or len(key.get_secret_value().encode()) < 32:
        raise ValueError("archive key unavailable")
    return hmac.new(key.get_secret_value().encode(), f"stage-archive:{stage_id}:{version}".encode(),
                    hashlib.sha256).hexdigest()[:24]


def create_encrypted_archive(stage_id: str, version: int, files: list[ProjectDeliveryFile]) -> str:
    if not files or sum(file.size_bytes for file in files) > MAX_PACKAGE_BYTES - 100_000:
        raise ValueError("invalid package size")
    key = uuid4().hex
    path = settings.uploads_dir.resolve() / key
    try:
        with path.open("xb") as output:
            path.chmod(0o600)
            with pyzipper.AESZipFile(output, "w", compression=pyzipper.ZIP_DEFLATED,
                                     encryption=pyzipper.WZ_AES) as package:
                package.setpassword(archive_password(stage_id, version).encode())
                package.setencryption(pyzipper.WZ_AES, nbits=256)
                for index, file in enumerate(files, 1):
                    name = f"{index:02d}-{safe_filename(file.original_name)}"
                    with asset_path(file.stored_key).open("rb") as source, package.open(name, "w") as entry:
                        shutil.copyfileobj(source, entry, length=128 * 1024)
        if path.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("package too large")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return key


def is_expired(stage: PaymentStage) -> bool:
    if not stage.deadline:
        return True
    deadline = stage.deadline
    return (deadline if deadline.tzinfo else deadline.replace(tzinfo=UTC)) <= now_utc()


def stage_payload(stage: PaymentStage) -> str:
    return f"{stage.id}:{stage.version}"


async def owned_stage(db: AsyncSession, chat: Conversation, stage_id: str, version: int, *, lock: bool = True):
    if chat.platform not in {"bale", "telegram"} or not chat.customer_id or chat.admin_id is not None:
        return None
    query = select(PaymentStage, Project).join(Project, Project.id == PaymentStage.project_id).where(
        PaymentStage.id == stage_id, PaymentStage.version == version, Project.staged_payment.is_(True),
        Project.customer_id == chat.customer_id, Project.conversation_id == chat.id,
        Project.archived_at.is_(None), Project.status.not_in(("declined", "canceled")),
    )
    if lock:
        query = query.with_for_update(of=PaymentStage)
    return (await db.execute(query)).one_or_none()


async def stage_job(db: AsyncSession, chat: Conversation, stage: PaymentStage, operation: str) -> None:
    key = f"{operation}:{stage_payload(stage)}"
    old = await db.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key == key))
    if old:
        if old.status in {"failed", "canceled"} or (old.status == "sent" and operation in {"stage_card", "stage_zip", "stage_secret"}):
            old.status, old.attempts, old.next_attempt_at = "pending", 0, now_utc()
        return
    db.add(OutboxMessage(
        dedupe_key=key, platform=chat.platform, chat_id=chat.chat_id, conversation_id=chat.id,
        operation=operation, body=stage_payload(stage),
    ))
