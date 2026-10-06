from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encode_access_token, verify_password
from app.models.user import User
from app.repositories.users import UserRepository


class AuthService:
    def __init__(self, session: AsyncSession):
        self.users = UserRepository(session)

    async def authenticate_user(self, email: str, password: str) -> User | None:
        user = await self.users.get_by_email(email)
        if user is None or not verify_password(password, user.password_hash):
            return None
        return user

    @staticmethod
    def create_access_token(user: User) -> str:
        return encode_access_token(user.id, user.auth_version)
