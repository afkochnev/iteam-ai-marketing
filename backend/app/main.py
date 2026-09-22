from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.rate_limit import SimpleRateLimitMiddleware
from app.core.request_id import RequestIdMiddleware

app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SimpleRateLimitMiddleware)
app.add_middleware(RequestIdMiddleware)
app.include_router(api_router, prefix="/api/v1")
register_error_handlers(app)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["system"])
async def root_readiness() -> dict[str, str]:
    from app.api.system import readiness

    return await readiness()
