from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db.base import Base
import db.models as _models  # noqa: F401 — registers all ORM models with Base.metadata
from settings import settings_obj

engine: AsyncEngine = create_async_engine(
    settings_obj.DATABASE_URL,
    echo=not settings_obj.IN_PRODUCTION,
    pool_pre_ping=True,
)
async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


_MIGRATIONS = [
    # Rich metadata columns added to bids_datasets (idempotent)
    "ALTER TABLE bids_datasets ADD COLUMN IF NOT EXISTS authors TEXT[]",
    "ALTER TABLE bids_datasets ADD COLUMN IF NOT EXISTS license TEXT",
    "ALTER TABLE bids_datasets ADD COLUMN IF NOT EXISTS doi TEXT",
    "ALTER TABLE bids_datasets ADD COLUMN IF NOT EXISTS paper_references TEXT[]",
    "ALTER TABLE bids_datasets ADD COLUMN IF NOT EXISTS funding TEXT[]",
    "ALTER TABLE bids_datasets ADD COLUMN IF NOT EXISTS description_text TEXT",
    # Widen license if it was previously created as VARCHAR(100)
    "ALTER TABLE bids_datasets ALTER COLUMN license TYPE TEXT",
]


async def create_all_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _MIGRATIONS:
            await conn.execute(text(stmt))
