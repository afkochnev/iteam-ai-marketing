from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash

from app.core.config import settings

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    return password_hash.verify(password, encoded_hash)


def encode_access_token(user_id: UUID, auth_version: int = 1) -> str:
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode(
        {"sub": str(user_id), "exp": expires_at, "auth_version": auth_version},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> UUID:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    subject = payload.get("sub")
    if not isinstance(subject, str):
        raise jwt.InvalidTokenError("Missing subject")
    return UUID(subject)


def decode_auth_version(token: str) -> int:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    version = payload.get("auth_version")
    if type(version) is not int or version < 1:
        raise jwt.InvalidTokenError("Invalid auth version")
    return version


def validate_password(password: str) -> str:
    if not password.strip() or not 12 <= len(password) <= 128:
        raise ValueError("Пароль должен содержать от 12 до 128 символов и не быть пустым.")
    return password
