from fastapi import APIRouter

from app.api.agent_runs import router as agent_runs_router
from app.api.agents import router as agents_router
from app.api.auth import router as auth_router
from app.api.campaigns import router as campaigns_router
from app.api.tasks import router as tasks_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(agents_router)
api_router.include_router(agent_runs_router)
api_router.include_router(campaigns_router)
api_router.include_router(tasks_router)


@api_router.get("/health", tags=["system"])
async def api_health() -> dict[str, str]:
    return {"status": "ok"}
