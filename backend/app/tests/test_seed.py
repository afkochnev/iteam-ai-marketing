from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.repositories.users import UserRepository
from app.seed import seed_admin


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    assert await seed_admin() is True
    assert await seed_admin() is False
    assert await UserRepository(db_session).get_by_email(settings.admin_email) is not None
