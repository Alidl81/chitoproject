from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bot.transport import BotAdapter, configured_adapters
from app.config import settings
from app.database import SessionLocal
from app.models import IncomingUpdate, PollerLease, now_utc

logger = logging.getLogger("projecthub.poller")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _claim_lease(platform: str, owner: str) -> bool:
    async with SessionLocal() as session:
        lease = await session.scalar(select(PollerLease).where(PollerLease.platform == platform).with_for_update())
        now = now_utc()
        if lease and _aware(lease.expires_at) > now and lease.owner != owner:
            return False
        if lease is None:
            session.add(PollerLease(platform=platform, owner=owner, expires_at=now + timedelta(seconds=90)))
        else:
            lease.owner = owner
            lease.expires_at = now + timedelta(seconds=90)
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def _renew_lease(platform: str, owner: str) -> bool:
    async with SessionLocal() as session:
        lease = await session.get(PollerLease, platform)
        if not lease or lease.owner != owner:
            return False
        lease.expires_at = now_utc() + timedelta(seconds=90)
        await session.commit()
        return True


async def _release_lease(platform: str, owner: str) -> None:
    async with SessionLocal() as session:
        lease = await session.get(PollerLease, platform)
        if lease and lease.owner == owner:
            lease.expires_at = now_utc()
            await session.commit()


async def _last_offset(platform: str) -> int | None:
    async with SessionLocal() as session:
        latest = await session.scalar(select(IncomingUpdate.update_id).where(
            IncomingUpdate.platform == platform).order_by(IncomingUpdate.id.desc()).limit(1))
    try:
        return int(latest) + 1 if latest is not None else None
    except (TypeError, ValueError):
        return None


async def _persist_updates(platform: str, updates: list[dict]) -> int | None:
    if not updates:
        return None
    max_id: int | None = None
    async with SessionLocal() as session:
        ids = [str(item.get("update_id")) for item in updates if item.get("update_id") is not None]
        existing = set((await session.scalars(select(IncomingUpdate.update_id).where(
            IncomingUpdate.platform == platform, IncomingUpdate.update_id.in_(ids)))).all()) if ids else set()
        for update in updates:
            raw_id = update.get("update_id")
            if raw_id is None:
                continue
            update_id = str(raw_id)
            try:
                numeric_id = int(raw_id)
                max_id = numeric_id if max_id is None else max(max_id, numeric_id)
            except (TypeError, ValueError):
                pass
            if update_id not in existing:
                session.add(IncomingUpdate(platform=platform, update_id=update_id, payload=update,
                                           status="pending", received_at=now_utc(), next_attempt_at=now_utc()))
        await session.commit()
    return max_id


async def _ack_callbacks(adapter: BotAdapter, updates: list[dict]) -> None:
    for update in updates:
        callback = update.get("callback_query")
        if not isinstance(callback, dict) or not callback.get("id"):
            continue
        try:
            await adapter.answer_callback(str(callback["id"]))
        except Exception as exc:
            logger.info("callback acknowledgement failed platform=%s error=%s", adapter.platform, type(exc).__name__)


async def run_bot(platform: str) -> None:
    adapter = configured_adapters().get(platform)
    if adapter is None:
        logger.info("connector disabled platform=%s", platform)
        return
    owner = secrets.token_urlsafe(18)
    if not await _claim_lease(platform, owner):
        logger.error("poller lease is already held platform=%s", platform)
        await adapter.close()
        return
    offset = await _last_offset(platform)
    logger.info("poller started platform=%s", platform)
    try:
        while True:
            if not await _renew_lease(platform, owner):
                logger.error("poller lease lost platform=%s", platform)
                break
            try:
                updates = await adapter.get_updates(offset, settings.bot_polling_timeout)
                max_id = await _persist_updates(platform, updates)
                if max_id is not None:
                    offset = max_id + 1
                await _ack_callbacks(adapter, updates)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("poll cycle failed platform=%s error=%s", platform, type(exc).__name__)
                await asyncio.sleep(3)
    finally:
        await _release_lease(platform, owner)
        await adapter.close()
