import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.rate_limit import SimpleRateLimitMiddleware
from app.core.request_id import RequestIdMiddleware
from app.core.security_headers import (
    OriginProtectionMiddleware,
    SecurityHeadersMiddleware,
)

settings.validate_production()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
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
async def health() -> dict[str, str]:
    return {"status": "ok", "version": settings.app_version}


@app.get("/health/ready", tags=["system"])
async def root_readiness() -> dict[str, str]:
    from app.api.system import readiness

    return await readiness()
