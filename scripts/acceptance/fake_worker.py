"""Run a real Celery AI worker with only the model boundary replaced."""

import asyncio
import json
from uuid import UUID

import redis
from agents.tool_context import ToolContext
from app.agents.social_tools import read_content_version
from app.core.config import settings
from app.core.database import create_worker_session_factory
from app.models.agent_run import AgentRun, AgentRunStatus
from app.services.agent_runner_service import AgentRunnerService, RuntimeResult
from app.workers.celery_app import celery_app
from celery.signals import task_prerun
from redis.asyncio import Redis


@task_prerun.connect
def record_delivery(task=None, args=None, **kwargs):
    if task is not None and task.name == "execute_agent_run":
        assert task.request.delivery_info["routing_key"] == "ai"
        redis.Redis.from_url(settings.redis_url).incr("acceptance:ai_deliveries")
        asyncio.run(observe_before_claim(UUID(args[0])))


async def observe_before_claim(run_id):
    worker_engine, factory = create_worker_session_factory()
    try:
        async with factory() as session:
            run = await session.get(AgentRun, run_id)
            if run.status == AgentRunStatus.QUEUED:
                redis.Redis.from_url(settings.redis_url).set(
                    f"acceptance:seen_queued:{run_id}", "1"
                )
    finally:
        await worker_engine.dispose()


async def fake_run(self, snapshot, task_input, context, trace_id):
    async with Redis.from_url(settings.redis_url) as client:
        await client.incr("acceptance:fake_calls")
        # Leave the real claimed run RUNNING until the scenario releases it.
        for _ in range(600):
            if await client.get(f"acceptance:release:{context.agent_run_id}"):
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Acceptance did not release the claimed run")
    version_id = context.allowed_content_version_ids[0]
    tool_args = json.dumps({"content_version_id": str(version_id)})
    await read_content_version.on_invoke_tool(
        ToolContext(
            context,
            tool_name="read_content_version",
            tool_call_id="acceptance-read",
            tool_arguments=tool_args,
        ),
        tool_args,
    )
    return RuntimeResult(
        {
            "sufficient": True,
            "pack": {
                "strategy_summary": "Deterministic acceptance result",
                "posts": [
                    {
                        "key": f"post_{index}",
                        "channel": "TELEGRAM",
                        "title": f"Post {index}",
                        "text_markdown": f"Grounded revision {index}",
                        "cta": "Read more",
                        "sources": [
                            {
                                "content_version_id": str(version_id),
                                "section_key": "problem",
                            }
                        ],
                        "suggested_publish_order": index,
                    }
                    for index in range(1, 6)
                ],
            },
            "gaps": [],
        },
        0,
        0,
        0,
        0,
        None,
    )


AgentRunnerService.run = fake_run
if __name__ == "__main__":
    celery_app.worker_main(
        [
            "worker",
            "--queues=ai",
            "--concurrency=2",
            "--loglevel=INFO",
            "--hostname=ai-acceptance@%h",
        ]
    )
