from fastapi import APIRouter, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, SessionDependency
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import hash_password, verify_password
from app.models.user import User, UserRole
from app.repositories.users import UserRepository
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    RegisterRequest,
    UserResponse,
)
from app.services.activity_log_service import ActivityLogService
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest, response: Response, session: SessionDependency
) -> LoginResponse:
    service = AuthService(session)
    user = await service.authenticate_user(str(payload.email), payload.password)
    if user is None or not user.is_active:
        raise AppError(
            "INVALID_CREDENTIALS",
            "Неверный email или пароль.",
            status.HTTP_401_UNAUTHORIZED,
        )
    set_session_cookie(response, user)
    return LoginResponse(user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUser) -> User:
    return current_user


@router.post("/logout", response_model=LogoutResponse)
async def logout(response: Response) -> LogoutResponse:
    response.delete_cookie(settings.auth_cookie_name, path="/", samesite="lax")
    return LogoutResponse(message="Вы вышли из системы.")


def set_session_cookie(response: Response, user: User) -> None:
    token = AuthService.create_access_token(user)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


@router.get("/capabilities")
async def capabilities() -> dict[str, bool]:
    return {"self_registration_enabled": settings.self_registration_enabled}


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(payload: RegisterRequest, session: SessionDependency) -> User:
    if not settings.self_registration_enabled:
        raise AppError("REGISTRATION_DISABLED", "Самостоятельная регистрация отключена.", 403)
    try:
        user = await UserRepository(session).create(
            email=str(payload.email),
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            role=UserRole.MANAGER,
        )
    except IntegrityError:
        await session.rollback()
        raise AppError("EMAIL_UNAVAILABLE", "Этот email недоступен для регистрации.", 409) from None
    await ActivityLogService(session).record("USER_REGISTERED", user_id=user.id)
    await session.commit()
    return user


@router.post("/change-password", response_model=LogoutResponse)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: CurrentUser,
    response: Response,
    session: SessionDependency,
) -> LogoutResponse:
    # Serialize concurrent changes and recheck the authenticated version under the lock.
    authenticated_version = current_user.auth_version
    user = await session.scalar(
        select(User)
        .where(User.id == current_user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active or user.auth_version != authenticated_version:
        raise AppError("AUTHENTICATION_REQUIRED", "Требуется авторизация.", 401)
    if not verify_password(payload.current_password, user.password_hash):
        raise AppError("INVALID_CURRENT_PASSWORD", "Текущий пароль неверен.", 400)
    if verify_password(payload.new_password, user.password_hash):
        raise AppError("PASSWORD_UNCHANGED", "Новый пароль должен отличаться от текущего.", 400)
    user.password_hash = hash_password(payload.new_password)
    user.auth_version += 1
    await ActivityLogService(session).record("USER_PASSWORD_CHANGED", user_id=user.id)
    await session.commit()
    set_session_cookie(response, user)
    return LogoutResponse(message="Пароль изменён. Другие сессии завершены.")
