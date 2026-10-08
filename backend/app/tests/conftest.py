import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

# Resolve and validate the test URL before importing app.core.database.  That
# module creates its engine at import time; ordering here prevents tests from
# ever binding the destructive fixture to the runtime database.
from app.core.config import settings
from app.core.test_database import resolve_test_database_url
from app.core.test_redis import resolve_test_redis_url

_TEST_DATABASE_URL = resolve_test_database_url(
    settings.database_url,
    os.environ.get("TEST_DATABASE_URL"),
)
_TEST_REDIS_URL = resolve_test_redis_url(
    settings.redis_url,
    os.environ.get("TEST_REDIS_URL"),
)
settings.app_env = "test"
settings.database_url = _TEST_DATABASE_URL
settings.redis_url = _TEST_REDIS_URL
settings.validate_redis_isolation()
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL
os.environ["TEST_DATABASE_URL"] = _TEST_DATABASE_URL
os.environ["REDIS_URL"] = _TEST_REDIS_URL
os.environ["TEST_REDIS_URL"] = _TEST_REDIS_URL

from app.core.database import (  # noqa: E402
    async_session_factory,
    engine,
    get_db_session,
)
from app.main import app  # noqa: E402
from app.models.agent import Agent  # noqa: E402
from app.models.agent_run import AgentRun, ToolCall  # noqa: E402
from app.models.approval import Approval  # noqa: E402
from app.models.campaign import Campaign  # noqa: E402
from app.models.campaign_kpi import CampaignKPI  # noqa: E402
from app.models.content import (  # noqa: E402
    ContentDerivation,
    ContentItem,
    ContentVersion,
    ContentVersionSource,
)
from app.models.knowledge import KnowledgeItem, KnowledgeSource, KnowledgeStore  # noqa: E402
from app.models.knowledge_pack import KnowledgePack, KnowledgePackItem  # noqa: E402
from app.models.marketing_chat import (  # noqa: E402
    MarketingContextSnapshot,
    MarketingConversation,
    MarketingMessage,
)
from app.models.marketing_experiment import (  # noqa: E402
    MarketingExperiment,
    MarketingExperimentPublication,
)
from app.models.marketing_feedback import (  # noqa: E402
    MarketingFeedback,
    MarketingFeedbackAnalysis,
)
from app.models.optimization import (  # noqa: E402
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
)
from app.models.publication import Publication, PublicationReconciliation  # noqa: E402
from app.models.publication_metrics import PublicationMetricsSnapshot  # noqa: E402
from app.models.publication_plan import PublicationPlan, PublicationPlanItem  # noqa: E402
from app.models.task import Task, TaskDependency  # noqa: E402
from app.models.user import User  # noqa: E402


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    await engine.dispose()
    async with async_session_factory() as session:
        await session.execute(delete(MarketingMessage))
        await session.execute(delete(MarketingContextSnapshot))
        await session.execute(delete(MarketingConversation))
        await session.execute(update(ContentItem).values(current_version_id=None))
        await session.execute(update(Task).values(optimization_action_id=None))
        await session.execute(
            update(PublicationPlan).values(
                optimization_action_id=None,
                optimization_proposal_id=None,
                feedback_analysis_id=None,
            )
        )
        await session.execute(delete(MarketingExperimentPublication))
        await session.execute(delete(MarketingExperiment))
        await session.execute(delete(CampaignOptimizationAction))
        await session.execute(delete(CampaignOptimizationProposal))
        await session.execute(delete(MarketingFeedbackAnalysis))
        await session.execute(delete(MarketingFeedback))
        await session.execute(delete(PublicationReconciliation))
        await session.execute(delete(PublicationPlanItem))
        await session.execute(delete(PublicationPlan))
        await session.execute(delete(PublicationMetricsSnapshot))
        await session.execute(delete(Publication))
        await session.execute(delete(ContentDerivation))
        await session.execute(delete(ContentVersionSource))
        await session.execute(delete(ContentVersion))
        await session.execute(delete(ContentItem))
        await session.execute(delete(KnowledgePackItem))
        await session.execute(delete(KnowledgePack))
        await session.execute(delete(ToolCall))
        await session.execute(delete(KnowledgeItem))
        await session.execute(delete(KnowledgeSource))
        await session.execute(delete(KnowledgeStore))
        await session.execute(delete(Approval))
        await session.execute(delete(AgentRun))
        await session.execute(delete(TaskDependency))
        await session.execute(delete(Task))
        await session.execute(delete(CampaignKPI))
        await session.execute(delete(Campaign))
        await session.execute(delete(Agent))
        await session.execute(delete(User))
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(delete(MarketingMessage))
        await session.execute(delete(MarketingContextSnapshot))
        await session.execute(delete(MarketingConversation))
        await session.execute(update(ContentItem).values(current_version_id=None))
        await session.execute(update(Task).values(optimization_action_id=None))
        await session.execute(
            update(PublicationPlan).values(
                optimization_action_id=None,
                optimization_proposal_id=None,
                feedback_analysis_id=None,
            )
        )
        await session.execute(delete(MarketingExperimentPublication))
        await session.execute(delete(MarketingExperiment))
        await session.execute(delete(CampaignOptimizationAction))
        await session.execute(delete(CampaignOptimizationProposal))
        await session.execute(delete(MarketingFeedbackAnalysis))
        await session.execute(delete(MarketingFeedback))
        await session.execute(delete(PublicationReconciliation))
        await session.execute(delete(PublicationPlanItem))
        await session.execute(delete(PublicationPlan))
        await session.execute(delete(PublicationMetricsSnapshot))
        await session.execute(delete(Publication))
        await session.execute(delete(ContentDerivation))
        await session.execute(delete(ContentVersionSource))
        await session.execute(delete(ContentVersion))
        await session.execute(delete(ContentItem))
        await session.execute(delete(KnowledgePackItem))
        await session.execute(delete(KnowledgePack))
        await session.execute(delete(ToolCall))
        await session.execute(delete(KnowledgeItem))
        await session.execute(delete(KnowledgeSource))
        await session.execute(delete(KnowledgeStore))
        await session.execute(delete(Approval))
        await session.execute(delete(AgentRun))
        await session.execute(delete(TaskDependency))
        await session.execute(delete(Task))
        await session.execute(delete(CampaignKPI))
        await session.execute(delete(Campaign))
        await session.execute(delete(Agent))
        await session.execute(delete(User))
        await session.commit()
    await engine.dispose()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def block_live_provider_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail before any test can resolve a real provider (mock transports still work)."""
    import socket

    original = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host)
        if any(
            name == domain or name.endswith("." + domain)
            for domain in ["openai.com", "telegram.org", "vk.com", "vk.ru"]
        ):
            raise AssertionError("Live provider network access is forbidden in tests")
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
