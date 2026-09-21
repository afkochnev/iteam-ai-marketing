from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory, engine, get_db_session
from app.main import app
from app.models.agent import Agent
from app.models.campaign import Campaign
from app.models.user import User


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    await engine.dispose()
    async with async_session_factory() as session:
        await session.execute(delete(Campaign))
        await session.execute(delete(Agent))
        await session.execute(delete(User))
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(delete(Campaign))
        await session.execute(delete(Agent))
        await session.execute(delete(User))
        await session.commit()
    await engine.dispose()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()
