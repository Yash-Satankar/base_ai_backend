# app/db/database.py
"""
Async database connection pool and session lifecycle management.
Uses SQLAlchemy asyncpg dialect for PostgreSQL metadata storage.
"""

import logging
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings
from app.db.models import Base

logger = logging.getLogger(__name__)

# Ensure the database URL uses the asyncpg driver
db_url = settings.DATABASE_URL
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")

# Create Async Engine
engine = create_async_engine(
    db_url,
    echo=False,  # Set to True only when debugging SQL queries
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

# Async Session Factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection helper for routes/handlers to obtain an async session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Creates metadata tables in PostgreSQL database if they do not exist."""
    try:
        async with engine.begin() as conn:
            # Create all tables defined in models.py
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ PostgreSQL database metadata tables initialized successfully")
    except Exception as e:
        logger.error(f"❌ Failed to initialize database tables: {e}", exc_info=True)
        raise
