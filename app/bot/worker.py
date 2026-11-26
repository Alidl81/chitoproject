from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError

from app.bot.flow import handle_update
from app.bot.transport import BotAdapter, ProviderError, configured_adapters
from app.config import settings
from app.database import SessionLocal
from app.models import (
    Conversation,
    ConversationFile,
    ConversationMessage,
    IncomingUpdate,
    OutboxMessage,
    PollerLease,
    now_utc,
)

logger = logging.getLogger("chitoproject.worker")
MAX_ATTEMPTS = 12
INBOX_LEASE = timedelta(minutes=5)
OUTBOX_LEASE = timedelta(minutes=3)
WORKER_LEASE = timedelta(seconds=90)
WORKER_LEASE_RETRY_SECONDS = 3


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _retry_seconds(attempts: int, retry_after: int | None = None) -> int:
    if retry_after is not None:
        return max(1, min(3600, retry_after))
    return min(900, 2 ** min(max(attempts, 1), 10))


async def _worker_lease(owner: str, *, release: bool = False) -> bool:
    async with SessionLocal() as session:
        row = await session.scalar(select(PollerLease).where(PollerLease.platform == "worker").with_for_update())
        now = now_utc()
        if release:
            if row and row.owner == owner:
                row.expires_at = now
                await session.commit()
            return True
        if row and _aware(row.expires_at) > now and row.owner != owner:
            return False
        if row is None:
            session.add(PollerLease(platform="worker", owner=owner, expires_at=now + WORKER_LEASE))
        else:
            row.owner = owner
            row.expires_at = now + WORKER_LEASE
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def _worker_heartbeat(owner: str, stopping: asyncio.Event, lost: asyncio.Event) -> None:
    while not stopping.is_set():
        try:
            await asyncio.wait_for(stopping.wait(), timeout=25)
            return
        except TimeoutError:
            if not await _worker_lease(owner):
                lost.set()
                return


def _chat_key(payload: dict[str, Any]) -> str:
    callback = payload.get("callback_query") or {}
    message = callback.get("message") if isinstance(callback, dict) else None
    message = message or payload.get("message") or payload.get("edited_message") or {}
    chat = message.get("chat") if isinstance(message, dict) else {}
    return str(chat.get("id", "unknown"))


async def _claim_incoming(limit: int = 100) -> list[tuple[int, str, str, dict[str, Any]]]:
    now = now_utc()
    async with SessionLocal() as session:
        query = select(IncomingUpdate).where(
            or_(
                (IncomingUpdate.status == "pending") & (IncomingUpdate.next_attempt_at <= now),
                (IncomingUpdate.status == "processing") & (IncomingUpdate.lease_until.is_not(None)) & (IncomingUpdate.lease_until <= now),
            )
        ).order_by(IncomingUpdate.id).with_for_update(skip_locked=True).limit(limit)
        rows = list((await session.scalars(query)).all())
        batch = []
        for row in rows:
            row.status = "processing"
            row.lease_until = now + INBOX_LEASE
            row.attempts += 1
            batch.append((row.id, row.platform, row.update_id, row.payload))
        await session.commit()
        return batch


async def _process_update(item: tuple[int, str, str, dict[str, Any]], adapters: dict[str, BotAdapter]) -> bool:
    row_id, platform, update_id, payload = item
    adapter = adapters.get(platform)
    if adapter is None:
        await _defer_update(row_id, "connector_unavailable", 300)
        return False
    try:
        async with SessionLocal() as session:
            row = await session.get(IncomingUpdate, row_id)
            if not row or row.status != "processing":
                return False
            await handle_update(session, platform, payload, adapter)
            row.status = "done"
            row.processed_at = now_utc()
            row.lease_until = None
            row.payload = {}  # transcript is retained separately; discard raw provider metadata after processing
            row.last_error = None
            await session.commit()
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("update failed platform=%s update=%s error=%s", platform, update_id, type(exc).__name__)
        await _defer_update(row_id, type(exc).__name__)
        return False


async def _defer_update(row_id: int, error: str, delay: int | None = None) -> None:
    async with SessionLocal() as session:
        row = await session.get(IncomingUpdate, row_id)
        if not row:
            return
        row.last_error = error[:120]
        row.lease_until = None
        if row.attempts >= MAX_ATTEMPTS:
            row.status = "failed"
            row.payload = {}
        else:
            row.status = "pending"
            row.next_attempt_at = now_utc() + timedelta(seconds=delay or _retry_seconds(row.attempts))
        await session.commit()


async def process_incoming_batch(adapters: dict[str, BotAdapter]) -> int:
    batch = await _claim_incoming()
    if not batch:
        return 0
    groups: dict[tuple[str, str], list[tuple[int, str, str, dict[str, Any]]]] = defaultdict(list)
    for item in batch:
        groups[(item[1], _chat_key(item[3]))].append(item)
    semaphore = asyncio.Semaphore(settings.bot_worker_concurrency)
    processed = 0

    async def process_group(items: list[tuple[int, str, str, dict[str, Any]]]) -> int:
        nonlocal processed
        async with semaphore:
            local = 0
            for item in items:
                if await _process_update(item, adapters):
                    local += 1
            return local

    ordered_groups = []
    for items in groups.values():
        items.sort(key=lambda item: int(item[2]) if item[2].isdigit() else item[0])
        ordered_groups.append(items)
    results = await asyncio.gather(*(process_group(items) for items in ordered_groups), return_exceptions=True)
    for result in results:
        if isinstance(result, int):
            processed += result
        elif isinstance(result, Exception):
            logger.warning("inbox group failed error=%s", type(result).__name__)
    return processed


async def _claim_outbox(limit: int = 100) -> list[int]:
    now = now_utc()
    async with SessionLocal() as session:
        query = select(OutboxMessage).where(
            or_(
                (OutboxMessage.status == "pending") & (OutboxMessage.next_attempt_at <= now),
                (OutboxMessage.status == "processing") & (OutboxMessage.lease_until.is_not(None)) & (OutboxMessage.lease_until <= now),
            )
        ).order_by(OutboxMessage.id).with_for_update(skip_locked=True).limit(limit)
        rows = list((await session.scalars(query)).all())
        ids = []
        for row in rows:
            row.status = "processing"
            row.lease_until = now + OUTBOX_LEASE
            row.attempts += 1
            ids.append(row.id)
        await session.commit()
        return ids


async def _dispatch_outbox(row_id: int, adapters: dict[str, BotAdapter]) -> bool:
    async with SessionLocal() as session:
        row = await session.get(OutboxMessage, row_id)
        if not row or row.status != "processing":
            return False
        adapter = adapters.get(row.platform)
        if adapter is None:
            await _defer_outbox(session, row, "connector_unavailable", 300, permanent=False)
            await session.commit()
            return False
        conversation = await session.get(Conversation, row.conversation_id)
        transcript = await session.get(ConversationMessage, row.transcript_message_id) if row.transcript_message_id else None
        try:
            message_id = row.provider_message_id
            operation = row.operation
            if row.use_as_ui and conversation:
                if row.force_send:
                    operation = "send"
                    message_id = None
                elif conversation.ui_message_id:
                    operation = "edit"
                    message_id = conversation.ui_message_id
                else:
                    operation = "send"
                    message_id = None
            if operation == "send":
                sent_id = await adapter.send_message(row.chat_id, row.body, row.keyboard)
                message_id = sent_id
            elif operation == "document":
                archived = await session.scalar(select(ConversationFile).where(
                    ConversationFile.message_id == row.transcript_message_id,
                    ConversationFile.conversation_id == row.conversation_id,
                )) if transcript else None
                if not archived or not re.fullmatch(r"[a-f0-9]{32}", archived.stored_key):
                    raise ProviderError("missing_document", permanent=True)
                root = settings.uploads_dir.resolve()
                path = (root / archived.stored_key).resolve()
                if path.parent != root or not path.is_file():
                    raise ProviderError("missing_document", permanent=True)
                message_id = await adapter.send_document(
                    row.chat_id, path, archived.original_name, archived.mime_type, row.body,
                )
            elif operation == "edit":
                if not message_id:
                    raise ProviderError("missing_edit_target", permanent=True)
                try:
                    await adapter.edit_message_text(row.chat_id, message_id, row.body, row.keyboard)
                except ProviderError as exc:
                    if not exc.permanent:
                        raise
                    sent_id = await adapter.send_message(row.chat_id, row.body, row.keyboard)
                    message_id = sent_id
                    operation = "send"
            elif operation == "review_del":
                message_id = transcript.provider_message_id if transcript else None
                if not message_id:
                    # The preview send may still be waiting for a provider retry.
                    original = await session.scalar(select(OutboxMessage).where(
                        OutboxMessage.transcript_message_id == row.transcript_message_id,
                        OutboxMessage.operation == "send",
                    ).limit(1))
                    if original and original.status in {"pending", "processing"}:
                        raise ProviderError("preview_send_pending", retry_after=15)
                elif not await adapter.delete_message(row.chat_id, message_id):
                    raise ProviderError("delete_not_confirmed", permanent=True)
            elif operation == "delete":
                if not message_id or not await adapter.delete_message(row.chat_id, message_id):
                    raise ProviderError("delete_not_confirmed", permanent=True)
            else:
                raise ProviderError("unknown_outbox_operation", permanent=True)

            row.status = "sent"
            row.sent_at = now_utc()
            row.lease_until = None
            row.last_error = None
            if row.use_as_ui and conversation and operation in {"send", "edit"}:
                conversation.ui_message_id = message_id
            if transcript and operation == "review_del" and message_id:
                transcript.deleted_from_chat = True
            if transcript and row.operation != "review_del":
                meta = dict(transcript.metadata_json or {})
                meta["delivery_status"] = "sent"
                transcript.metadata_json = meta
                if operation in {"send", "edit", "document"}:
                    if transcript.provider_message_id != message_id and message_id:
                        current_revision = await session.scalar(select(func.max(ConversationMessage.revision)).where(
                            ConversationMessage.conversation_id == transcript.conversation_id,
                            ConversationMessage.provider_message_id == message_id,
                            ConversationMessage.id != transcript.id,
                        ))
                        transcript.revision = int(current_revision or 0) + 1
                    transcript.provider_message_id = message_id
            if row.operation == "delete":
                await session.execute(
                    sql_update(ConversationMessage)
                    .where(
                        ConversationMessage.conversation_id == row.conversation_id,
                        ConversationMessage.provider_message_id == row.provider_message_id,
                    )
                    .values(deleted_from_chat=True)
                )
            await session.commit()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            permanent = isinstance(exc, ProviderError) and exc.permanent
            retry_after = exc.retry_after if isinstance(exc, ProviderError) else None
            _defer_outbox(session, row, type(exc).__name__, _retry_seconds(row.attempts, retry_after), permanent=permanent)
            if transcript and operation == "review_del" and message_id:
                transcript.deleted_from_chat = True
            if transcript and row.operation != "review_del":
                meta = dict(transcript.metadata_json or {})
                meta["delivery_status"] = "failed" if permanent else "retrying"
                transcript.metadata_json = meta
            await session.commit()
            logger.info("outbox delivery failed platform=%s id=%s error=%s", row.platform, row.id, type(exc).__name__)
            return False


def _defer_outbox(session, row: OutboxMessage, error: str, delay: int, *, permanent: bool) -> None:
    row.last_error = error[:120]
    row.lease_until = None
    if permanent or row.attempts >= MAX_ATTEMPTS:
        row.status = "failed"
    else:
        row.status = "pending"
        row.next_attempt_at = now_utc() + timedelta(seconds=delay)


async def process_outbox_batch(adapters: dict[str, BotAdapter]) -> int:
    ids = await _claim_outbox()
    if not ids:
        return 0
    async with SessionLocal() as session:
        rows = list((await session.scalars(select(OutboxMessage).where(OutboxMessage.id.in_(ids)).order_by(OutboxMessage.id))).all())
        groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for row in rows:
            groups[(row.platform, row.chat_id)].append(row.id)
    semaphore = asyncio.Semaphore(settings.bot_worker_concurrency)

    async def dispatch_group(group_ids: list[int]) -> int:
        async with semaphore:
            sent = 0
            for index, row_id in enumerate(group_ids):
                if await _dispatch_outbox(row_id, adapters):
                    sent += 1
                else:
                    async with SessionLocal() as session:
                        current = await session.get(OutboxMessage, row_id)
                        should_pause = bool(current and current.status in {"pending", "processing"})
                        if should_pause:
                            await session.execute(
                                sql_update(OutboxMessage)
                                .where(OutboxMessage.id.in_(group_ids[index + 1:]),
                                       OutboxMessage.status == "processing")
                                .values(status="pending", lease_until=None,
                                        next_attempt_at=now_utc() + timedelta(seconds=2))
                            )
                            await session.commit()
                            break
                if index < len(group_ids) - 1:
                    row = next((item for item in rows if item.id == row_id), None)
                    await asyncio.sleep(1.05 if row and row.operation != "delete" else 0.2)
            return sent

    results = await asyncio.gather(*(dispatch_group(group) for group in groups.values()), return_exceptions=True)
    return sum(result for result in results if isinstance(result, int))


async def run_worker() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    adapters = configured_adapters()
    owner = secrets.token_urlsafe(18)
    try:
        acquired = await _worker_lease(owner)
        if not acquired:
            logger.info("worker lease is held; waiting for it to expire")
        while not acquired:
            await asyncio.sleep(WORKER_LEASE_RETRY_SECONDS)
            acquired = await _worker_lease(owner)
    except BaseException:
        for adapter in adapters.values():
            await adapter.close()
        raise
    stopping = asyncio.Event()
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_worker_heartbeat(owner, stopping, lost))
    logger.info("worker started concurrency=%s", settings.bot_worker_concurrency)
    try:
        while not lost.is_set():
            try:
                inbound = await process_incoming_batch(adapters)
                outbound = await process_outbox_batch(adapters)
                if inbound or outbound:
                    logger.info("worker batch processed incoming=%s outgoing=%s", inbound, outbound)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("worker cycle failed error=%s", type(exc).__name__)
            await asyncio.sleep(0.5)
    finally:
        stopping.set()
        await heartbeat
        await _worker_lease(owner, release=True)
        for adapter in adapters.values():
            await adapter.close()
