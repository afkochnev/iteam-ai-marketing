from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent_run import AgentRun, AgentRunStatus


class AgentRunRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, values: Mapping[str, Any]) -> AgentRun:
        run = AgentRun(**values)
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_by_id(self, run_id: UUID, *, lock: bool = False) -> AgentRun | None:
        query = (
            select(AgentRun)
            .options(selectinload(AgentRun.agent), selectinload(AgentRun.tool_calls))
            .where(AgentRun.id == run_id)
        )
        if lock:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_runs(
        self,
        *,
        task_id: UUID | None = None,
        agent_id: UUID | None = None,
        campaign_id: UUID | None = None,
        status: AgentRunStatus | None = None,
    ) -> list[AgentRun]:
        query = select(AgentRun).options(
            selectinload(AgentRun.agent), selectinload(AgentRun.tool_calls)
        )
        for condition in (
            AgentRun.task_id == task_id if task_id else None,
            AgentRun.agent_id == agent_id if agent_id else None,
            AgentRun.campaign_id == campaign_id if campaign_id else None,
            AgentRun.status == status if status else None,
        ):
            if condition is not None:
                query = query.where(condition)
        result = await self.session.execute(
            query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        )
        return list(result.scalars().unique())

    async def get_active_for_task(self, task_id: UUID) -> AgentRun | None:
        result = await self.session.execute(
            select(AgentRun).where(
                AgentRun.task_id == task_id,
                AgentRun.status.in_(
                    [AgentRunStatus.QUEUED, AgentRunStatus.RUNNING, AgentRunStatus.WAITING_APPROVAL]
                ),
            )
        )
        return result.scalar_one_or_none()

    async def update(self, run: AgentRun, values: Mapping[str, Any]) -> None:
        for field, value in values.items():
            setattr(run, field, value)
        await self.session.flush()
