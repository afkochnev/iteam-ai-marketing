import logging

from fastapi import FastAPI

from app.core.config import settings
from app.core.runtime_admission import admission_status, is_passive_runtime


def _create_active_app() -> FastAPI:
    # These imports construct business routes and Celery producers. Passive
    # startup must never import them, even when no request will be served.
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    from app.api.router import api_router
    from app.core.errors import register_error_handlers
    from app.core.rate_limit import SimpleRateLimitMiddleware
    from app.core.request_id import RequestIdMiddleware
    from app.core.security_headers import (
        OriginProtectionMiddleware,
        SecurityHeadersMiddleware,
    )

    app = FastAPI(title=settings.app_name, version=settings.app_version)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if settings.app_env.lower() in {"production", "prod"}:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(OriginProtectionMiddleware)
    app.add_middleware(SimpleRateLimitMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router, prefix="/api/v1")
    register_error_handlers(app)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, object]:
        return {"status": "ok", "version": settings.app_version, **admission_status()}

    @app.get("/health/ready", tags=["system"])
    async def root_readiness() -> dict[str, object]:
        from app.api.system import readiness

        return await readiness()

    return app


# APP_ENV retains its validation semantics. Explicit passive admission is not
# an alternative environment that bypasses the production configuration checks.
settings.validate_production()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
if is_passive_runtime():
    from app.core.passive_runtime import create_passive_app

    app = create_passive_app()
else:
    app = _create_active_app()
