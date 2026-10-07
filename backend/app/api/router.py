from fastapi import APIRouter

from app.api.activities import router as activities_router
from app.api.agent_runs import router as agent_runs_router
from app.api.agents import router as agents_router
from app.api.approvals import router as approvals_router
from app.api.auth import router as auth_router
from app.api.campaigns import router as campaigns_router
from app.api.content import router as content_router
from app.api.feedback import router as feedback_router
from app.api.knowledge import router as knowledge_router
from app.api.knowledge_packs import router as knowledge_packs_router
from app.api.marketing_chat import router as marketing_chat_router
from app.api.metrics import router as metrics_router
from app.api.publication_plans import router as publication_plans_router
from app.api.publications import router as publications_router
from app.api.system import router as system_router
from app.api.tasks import router as tasks_router

api_router = APIRouter()
api_router.include_router(marketing_chat_router)
api_router.include_router(auth_router)
api_router.include_router(agents_router)
api_router.include_router(agent_runs_router)
api_router.include_router(activities_router)
api_router.include_router(approvals_router)
api_router.include_router(campaigns_router)
api_router.include_router(tasks_router)
api_router.include_router(knowledge_router)
api_router.include_router(knowledge_packs_router)
api_router.include_router(publications_router)
api_router.include_router(publication_plans_router)
api_router.include_router(metrics_router)
api_router.include_router(content_router)
api_router.include_router(feedback_router)
api_router.include_router(system_router)


@api_router.get("/health", tags=["system"])
async def api_health() -> dict[str, str]:
    return {"status": "ok"}
