from fastapi import APIRouter, Response, status

from app.api.dependencies import CurrentUser, SessionDependency
from app.core.config import settings
from app.core.errors import AppError
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, LogoutResponse, UserResponse
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
    token = service.create_access_token(user)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )
    return LoginResponse(user=UserResponse.model_validate(user))


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUser) -> User:
    return current_user


@router.post("/logout", response_model=LogoutResponse)
async def logout(response: Response) -> LogoutResponse:
    response.delete_cookie(settings.auth_cookie_name, path="/", samesite="lax")
    return LogoutResponse(message="Вы вышли из системы.")
