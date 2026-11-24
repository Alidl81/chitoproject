from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine_options: dict = {"pool_pre_ping": True}
if settings.database_url.startswith("postgresql+"):
    engine_options.update(pool_size=8, max_overflow=8, pool_timeout=15, pool_recycle=1800)
engine = create_async_engine(settings.database_url, **engine_options)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
