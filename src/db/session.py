"""
Async SQLAlchemy session factory and engine configuration.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event

from src.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",
    # SQLite-specific: allow multiple threads to access the same connection
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
    pool_pre_ping=True,
)

# Enable WAL mode for SQLite — allows concurrent readers during writes
if "sqlite" in settings.database_url:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_wal(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db() -> None:
    """Create all tables on startup."""
    from src.db.models import Base  # noqa: PLC0415 — avoid circular at module load

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async DB session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
