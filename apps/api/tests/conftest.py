import os

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://codeforge:codeforge@localhost:5433/codeforge")

@pytest_asyncio.fixture
async def setup_db():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()

@pytest_asyncio.fixture
async def session(setup_db):
    async_session = async_sessionmaker(setup_db, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session
