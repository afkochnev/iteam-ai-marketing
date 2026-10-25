from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.models.knowledge import KnowledgeSource
from app.models.user import User, UserRole
from app.repositories.users import UserRepository
from app.seed import seed_admin, seed_knowledge_sources


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    assert await seed_admin() is True
    assert await seed_admin() is False
    assert await UserRepository(db_session).get_by_email(settings.admin_email) is not None


async def test_knowledge_source_seed_is_idempotent(db_session: AsyncSession) -> None:
    assert await seed_knowledge_sources() is True
    assert await seed_knowledge_sources() is False
    sources = list((await db_session.scalars(select(KnowledgeSource))).all())
    assert len(sources) == 1
    assert sources[0].name == "Ручные загрузки"


async def test_seed_does_not_recreate_placeholder_after_admin_email_change(db_session):
    db_session.add(
        User(
            email="afkochnev@gmail.com",
            password_hash=hash_password("valid-password"),
            role=UserRole.ADMIN,
        )
    )
    await db_session.commit()
    assert await seed_admin() is False
    assert await UserRepository(db_session).get_by_email(settings.admin_email) is None
