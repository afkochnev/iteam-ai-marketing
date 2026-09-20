from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.user import UserRole
from app.repositories.users import UserRepository


async def test_create_user_normalizes_email_and_sets_defaults(db_session: AsyncSession) -> None:
    repository = UserRepository(db_session)
    user = await repository.create(
        email="  Admin@Example.COM ",
        password_hash=hash_password("password"),
        full_name="Admin",
        role=UserRole.ADMIN,
    )
    await db_session.commit()
    await db_session.refresh(user)
    assert user.email == "admin@example.com"
    assert user.role is UserRole.ADMIN
    assert user.is_active is True
    assert user.id is not None
    assert user.created_at.tzinfo is not None
    assert user.updated_at.tzinfo is not None
    assert await repository.get_by_email("ADMIN@example.com") == user
