"""Health-only ASGI application selected before importing any business runtime."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.core.runtime_admission import admission_status
from app.core.runtime_readiness import passive_readiness
from app.core.security_headers import SecurityHeadersMiddleware


class _HealthOnly:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})
            return
        if scope["type"] == "http" and (
            scope["method"] != "GET" or scope["path"] not in {"/health", "/health/ready"}
        ):
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {"code": "RUNTIME_PASSIVE", "message": "Runtime is passive."},
                    **admission_status(),
                },
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_passive_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if settings.app_env.lower() in {"production", "prod"}:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    app.add_middleware(SecurityHeadersMiddleware)
    # Outermost: no CORS preflight, redirect, auth or business middleware may
    # serve an otherwise disallowed method/path before the admission decision.
    app.add_middleware(_HealthOnly)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "version": settings.app_version, **admission_status()}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        payload = await passive_readiness()
        return JSONResponse(
            status_code=200 if payload["status"] == "ok" else 503,
            content=payload,
            headers={"Cache-Control": "no-store"},
        )

    return app
