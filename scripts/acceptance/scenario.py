"""Seed and verify an isolated runtime; never directly execute an AI task."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import settings
from app.core.database import async_session_factory, engine
from app.core.security import hash_password
from app.models.agent import Agent, AgentStatus, AgentTool
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import (
    ContentChannel,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.publication import Publication, PublicationStatus
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.schemas.task import TaskCreate
from app.services.task_service import TaskService
from app.workers.agent_worker import execute_agent_run
from redis.asyncio import Redis
from sqlalchemy import func, select


async def seed():
    assert settings.database_url.endswith("/iteam_acceptance")
    async with async_session_factory() as session:
        user = User(
            email="acceptance@example.com",
            password_hash=hash_password("synthetic-only"),
            full_name="Acceptance",
            role=UserRole.ADMIN,
        )
        agent = Agent(
            name="SMM Manager",
            slug="smm_manager",
            role="smm",
            system_prompt="Fake only",
            model="acceptance-fake",
            status=AgentStatus.ACTIVE,
            autonomy_level=2,
            settings={},
        )
        session.add_all([user, agent])
        await session.flush()
        session.add(
            AgentTool(
                agent_id=agent.id, tool_name="read_content_version", is_enabled=True
            )
        )
        campaign = Campaign(
            name="Isolated autonomous acceptance",
            goal="No provider calls",
            status=CampaignStatus.ACTIVE,
            created_by=user.id,
            strategy={"social_strategy": {"channels": ["TELEGRAM"], "post_count": 5}},
        )
        session.add(campaign)
        await session.flush()
        source_task = Task(
            campaign_id=campaign.id,
            title="Approved source",
            task_type=TaskType.MANUAL,
            status=TaskStatus.COMPLETED,
            assigned_agent_id=agent.id,
            input_data={},
            output_data={},
            retry_count=0,
        )
        session.add(source_task)
        await session.flush()
        items = []
        versions = []
        for content_type, title, channel in (
            (ContentType.ARTICLE, "Approved article", None),
            (ContentType.SOCIAL_POST_PACK, "Approved pack", None),
            (ContentType.SOCIAL_POST, "Due approved post", ContentChannel.TELEGRAM),
        ):
            item = ContentItem(
                campaign_id=campaign.id,
                source_task_id=source_task.id,
                content_type=content_type,
                title=title,
                status=ContentStatus.APPROVED,
                author_agent_id=agent.id,
                channel=channel,
                metadata_={},
            )
            session.add(item)
            await session.flush()
            version = ContentVersion(
                content_item_id=item.id,
                version_number=1,
                content=title,
                structured_content={
                    "sections": [
                        {
                            "key": "problem",
                            "heading": "Problem",
                            "body_markdown": "Approved evidence",
                        }
                    ]
                },
                created_by_agent_id=agent.id,
            )
            session.add(version)
            await session.flush()
            item.current_version_id = version.id
            items.append(item)
            versions.append(version)
        now = datetime.now(UTC)
        publication = Publication(
            campaign_id=campaign.id,
            content_item_id=items[2].id,
            content_version_id=versions[2].id,
            channel=ContentChannel.TELEGRAM,
            status=PublicationStatus.SCHEDULED,
            scheduled_at=now - timedelta(minutes=1),
            approved_for_publish_at=now,
            approved_for_publish_by=user.id,
        )
        session.add(publication)
        await session.commit()
        revision = await TaskService(session).create_task(
            TaskCreate(
                campaign_id=campaign.id,
                title="Autonomous pack revision",
                task_type=TaskType.CONTENT_REVISION,
                assigned_agent_id=agent.id,
                input_data={
                    "content_item_id": str(items[1].id),
                    "base_content_version_id": str(versions[1].id),
                    "revision_target_type": ContentType.SOCIAL_POST_PACK.value,
                    "revision_comment": "Clarify all posts",
                    "immutable_source_context": {
                        "article_version_ids": [str(versions[0].id)]
                    },
                },
            )
        )
        assert revision.status == TaskStatus.READY
        return {
            "task_id": str(revision.id),
            "publication_id": str(publication.id),
            "campaign_id": str(campaign.id),
            "content_id": str(items[1].id),
            "base_content_items": 3,
            "base_content_versions": 3,
        }


async def verify(ids):
    task_id, publication_id = UUID(ids["task_id"]), UUID(ids["publication_id"])
    async with Redis.from_url(settings.redis_url) as broker:
        # A genuine Beat tick must create and enqueue the run; do not call dispatcher here.
        for _ in range(400):
            async with async_session_factory() as session:
                run = await session.scalar(
                    select(AgentRun).where(AgentRun.task_id == task_id)
                )
                if run and run.status == AgentRunStatus.RUNNING:
                    run_id = run.id
                    break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Real scheduler/worker did not claim the READY task")
        assert await broker.get(f"acceptance:seen_queued:{run_id}")
        # The worker holds RUNNING at the fake provider boundary while several ticks pass.
        for _ in range(2):
            execute_agent_run.apply_async(args=[str(run_id)], queue="ai")
        await asyncio.sleep(settings.task_dispatch_interval_seconds * 4)
        async with async_session_factory() as session:
            runs = list(
                (
                    await session.scalars(
                        select(AgentRun).where(AgentRun.task_id == task_id)
                    )
                ).all()
            )
            assert len(runs) == 1 and runs[0].status == AgentRunStatus.RUNNING
        assert int(await broker.get("acceptance:fake_calls") or 0) == 1
        assert int(await broker.get("acceptance:ai_deliveries") or 0) >= 3
        await broker.set(f"acceptance:release:{run_id}", "1")
        for _ in range(400):
            async with async_session_factory() as session:
                run = await session.get(AgentRun, run_id)
                if run.status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED}:
                    assert run.status == AgentRunStatus.COMPLETED, run.error_code
                    break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Claimed run did not reach a terminal state")
        execute_agent_run.apply_async(args=[str(run_id)], queue="ai")
        await asyncio.sleep(settings.task_dispatch_interval_seconds * 4)
        async with async_session_factory() as session:
            run = await session.get(AgentRun, run_id)
            task = await session.get(Task, task_id)
            publication = await session.get(Publication, publication_id)
            assert task.status == TaskStatus.COMPLETED
            assert run.queue_job_id and run.started_at and run.completed_at
            assert publication.status == PublicationStatus.SCHEDULED
            assert publication.execution_token is None
            run_count = await session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.task_id == task_id)
            )
            version_count = await session.scalar(
                select(func.count())
                .select_from(ContentVersion)
                .where(ContentVersion.source_agent_run_id == run_id)
            )
            item_count = await session.scalar(
                select(func.count())
                .select_from(ContentItem)
                .where(ContentItem.source_task_id == task_id)
            )
            assert (run_count, version_count, item_count) == (1, 6, 5)
            assert int(await broker.get("acceptance:fake_calls") or 0) == 1
            provider_calls = int(await broker.get("acceptance:provider_calls") or 0)
            assert provider_calls == 0
            for queue in ("publication", "publication_control", "metrics"):
                assert await broker.llen(queue) == 0
            assert await broker.execute_command("DBSIZE") >= 0
            report = {
                **ids,
                "agent_run_id": str(run_id),
                "queue_job_id": run.queue_job_id,
                "queued_observed": True,
                "run_status": run.status.value,
                "task_status": task.status.value,
                "agent_run_count": run_count,
                "result_versions": version_count,
                "result_items": item_count,
                "real_ai_deliveries": int(
                    await broker.get("acceptance:ai_deliveries") or 0
                ),
                "fake_model_calls": 1,
                "provider_calls": provider_calls,
                "publication_status": publication.status.value,
                "publication_execution_token": None,
                "publication_queues_empty": True,
                "autonomous": "PASS",
                "idempotency": "PASS",
                "publication_isolation": "PASS",
            }
            return report


async def main():
    import sys

    if sys.argv[1] == "seed":
        report = await seed()
    else:
        report = await verify(json.loads(sys.argv[2]))
    await engine.dispose()
    print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
