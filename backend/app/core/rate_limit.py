from __future__ import annotations

from collections import defaultdict, deque
from time import monotonic

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import settings


class SimpleRateLimitMiddleware(BaseHTTPMiddleware):
    """Process-local guard for sensitive endpoints.

    Authentication and task execution are also protected by backend business
    authorization. This lightweight limiter prevents accidental request floods;
    it is deliberately not presented as a distributed quota service.
    """

    _shared_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._shared_hits.clear()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if settings.app_env != "production":
            return await call_next(request)
        category, limit = self._category(request)
        if category is None:
            return await call_next(request)
        key = (request.client.host if request.client else "unknown", category)
        now = monotonic()
        hits = self._shared_hits[key]
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= limit:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "Слишком много запросов. Повторите позже.",
                        "details": {},
                    }
                },
                headers={"Retry-After": "60"},
            )
        hits.append(now)
        return await call_next(request)

    @staticmethod
    def _category(request: Request) -> tuple[str | None, int]:
        if request.method != "POST":
            return None, 0
        path = request.url.path
        if path == "/api/v1/auth/register":
            return "registration", settings.rate_limit_registration_per_minute
        if path == "/api/v1/auth/change-password":
            return "password-change", settings.rate_limit_password_change_per_minute
        if path == "/api/v1/auth/login":
            return "login", settings.rate_limit_login_per_minute
        if path == "/api/v1/knowledge/upload":
            return "upload", settings.rate_limit_uploads_per_minute
        if path.endswith("/run") or path.endswith("/retry"):
            return "ai", settings.rate_limit_ai_actions_per_minute
        return None, 0
