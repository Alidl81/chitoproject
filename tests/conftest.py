from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SECRET_KEY", "test-key-for-projecthub-unit-tests-32-bytes")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base


@pytest_asyncio.fixture
async def db_context(tmp_path: Path, monkeypatch) -> AsyncIterator[tuple[AsyncSession, async_sessionmaker]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(settings, "uploads_dir", tmp_path)
    async with maker() as session:
        yield session, maker
    await engine.dispose()
