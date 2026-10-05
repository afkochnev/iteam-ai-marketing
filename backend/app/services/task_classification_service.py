from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.content import ContentItem, ContentType
from app.models.publication import Publication
from app.models.task import Task, TaskStatus, TaskType

TaskClassification = Literal["actionable", "superseded", "historical"]


def _default_classification(task: Task) -> TaskClassification:
    if task.status in {
        TaskStatus.NEW,
        TaskStatus.BLOCKED,
        TaskStatus.READY,
        TaskStatus.IN_PROGRESS,
        TaskStatus.WAITING_REVIEW,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.FAILED,
    }:
        return "actionable"
    return "historical"


async def classify_tasks(
    session: AsyncSession, tasks: Sequence[Task]
) -> dict[UUID, tuple[TaskClassification, str | None]]:
    """Classify task list rows without rewriting their durable status."""

    result: dict[UUID, tuple[TaskClassification, str | None]] = {
        task.id: (_default_classification(task), None) for task in tasks
    }
    failed_plan_tasks: list[tuple[Task, str]] = []
    for task in tasks:
        if (
            task.status is not TaskStatus.FAILED
            or task.task_type is not TaskType.CREATE_SOCIAL_POSTS
        ):
            continue
        plan_item_id = task.input_data.get("publication_plan_item_id")
        if plan_item_id:
            failed_plan_tasks.append((task, str(plan_item_id)))

    plan_item_ids = {plan_item_id for _, plan_item_id in failed_plan_tasks}
    if not plan_item_ids:
        return {
            task.id: (
                classification,
                "Историческая ошибка" if task.status is TaskStatus.FAILED else None,
            )
            for task in tasks
            for classification, _label in [result[task.id]]
        }

    successful_runs = (
        await session.execute(
            select(
                Task.input_data["publication_plan_item_id"].as_string(),
                Task.created_at,
                AgentRun.completed_at,
            )
            .join(AgentRun, AgentRun.task_id == Task.id)
            .where(
                Task.task_type == TaskType.CREATE_SOCIAL_POSTS,
                Task.status == TaskStatus.COMPLETED,
                AgentRun.status == AgentRunStatus.COMPLETED,
                Task.input_data["publication_plan_item_id"].as_string().in_(plan_item_ids),
            )
        )
    ).all()
    posts = (
        await session.execute(
            select(
                ContentItem.metadata_["publication_plan_item_id"].as_string(),
                ContentItem.created_at,
            ).where(
                ContentItem.content_type == ContentType.SOCIAL_POST,
                ContentItem.metadata_["publication_plan_item_id"].as_string().in_(plan_item_ids),
            )
        )
    ).all()
    publications = (
        await session.execute(
            select(
                ContentItem.metadata_["publication_plan_item_id"].as_string(),
                Publication.created_at,
            )
            .join(Publication, Publication.content_item_id == ContentItem.id)
            .where(
                ContentItem.content_type == ContentType.SOCIAL_POST,
                ContentItem.metadata_["publication_plan_item_id"].as_string().in_(plan_item_ids),
            )
        )
    ).all()

    for task, plan_item_id in failed_plan_tasks:
        has_later_success = any(
            candidate_plan_id == plan_item_id
            and task.created_at < success_task_created_at
            and completed_at is not None
            and task.created_at < completed_at
            for candidate_plan_id, success_task_created_at, completed_at in successful_runs
        )
        has_later_post = any(
            candidate_plan_id == plan_item_id and task.created_at < post_created_at
            for candidate_plan_id, post_created_at in posts
        )
        has_later_publication = any(
            candidate_plan_id == plan_item_id and task.created_at < publication_created_at
            for candidate_plan_id, publication_created_at in publications
        )
        if has_later_success or has_later_post or has_later_publication:
            result[task.id] = ("superseded", "Заменена успешным выполнением")

    return {
        task.id: (
            classification,
            label
            or (
                "Историческая ошибка"
                if classification == "historical" and task.status is TaskStatus.FAILED
                else None
            ),
        )
        for task in tasks
        for classification, label in [result[task.id]]
    }
