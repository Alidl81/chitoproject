from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from alembic.config import Config
from sqlalchemy import func, select

from alembic import command
from app.bot.transport import configured_adapters
from app.database import SessionLocal
from app.models import AdminUser, AuditEvent, OwnerActivation
from app.permissions import Role
from app.security import digest_token, expires_in, hash_password, now_utc, random_token


async def issue_owner_code() -> None:
    token = random_token(24)
    async with SessionLocal() as session:
        owner_count = await session.scalar(select(func.count(AdminUser.id)).where(AdminUser.role == Role.OWNER.value)) or 0
        if owner_count:
            raise SystemExit("The primary owner already exists; owner activation is disabled.")
        row = await session.get(OwnerActivation, 1)
        if row and row.used_at:
            raise SystemExit("The previous owner activation was consumed; refusing to reuse it.")
        if row is None:
            row = OwnerActivation(id=1, token_digest=digest_token(token), expires_at=expires_in(hours=24))
            session.add(row)
        else:
            row.token_digest = digest_token(token)
            row.created_at = now_utc()
            row.expires_at = expires_in(hours=24)
            row.used_at = None
        await session.commit()
    print("One-time owner activation code (valid for 24 hours):")
    print(token)
    print("Open /admin/activate over HTTPS and enter it. It will be consumed once.")


async def reset_password(username: str) -> None:
    temporary_password = "Aa1!" + random_token(24)
    async with SessionLocal() as session:
        admin = await session.scalar(select(AdminUser).where(AdminUser.username == username))
        if not admin:
            raise SystemExit("No matching team account exists.")
        admin.password_hash = hash_password(temporary_password)
        admin.session_version += 1
        db_event = AuditEvent(actor_admin_id=admin.id, action="auth.password_reset_cli", object_type="admin",
                              object_id=str(admin.id), detail={})
        session.add(db_event)
        await session.commit()
    print("One-time temporary password (share through a secure channel):")
    print(temporary_password)


async def check_bots() -> None:
    adapters = configured_adapters()
    if not adapters:
        raise SystemExit("No Telegram or Bale token is configured.")
    try:
        for platform, adapter in adapters.items():
            result = await adapter._api("getMe")
            name = str(result.get("username") or result.get("first_name") or "connected") if isinstance(result, dict) else "connected"
            print(f"{platform}: connected ({name})")
    finally:
        for adapter in adapters.values():
            await adapter.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="chitoproject")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply database migrations to head")
    sub.add_parser("issue-owner-code", help="Generate the first one-time owner activation code")
    reset = sub.add_parser("reset-password", help="Generate a temporary password for an existing team member")
    reset.add_argument("username")
    sub.add_parser("check-bots", help="Check configured bot credentials without printing tokens")
    args = parser.parse_args()
    if args.command == "migrate":
        cfg = Config(str(Path("alembic.ini").resolve()))
        command.upgrade(cfg, "head")
    elif args.command == "issue-owner-code":
        asyncio.run(issue_owner_code())
    elif args.command == "reset-password":
        asyncio.run(reset_password(args.username))
    elif args.command == "check-bots":
        asyncio.run(check_bots())


if __name__ == "__main__":
    main()
