from typing import Any, cast

from pydantic import BaseModel

from app.models.task import TaskType
from app.schemas.agent_outputs import ArticleWritingResult, CampaignPlan, KnowledgeResearchResult


class TaskOutputTypeRegistry:
    def get(self, task_type: TaskType) -> type[BaseModel] | None:
        mapping: dict[TaskType, type[BaseModel]] = {
            TaskType.CAMPAIGN_PLANNING: CampaignPlan,
            TaskType.KNOWLEDGE_RESEARCH: KnowledgeResearchResult,
            TaskType.WRITE_ARTICLE: ArticleWritingResult,
        }
        return mapping.get(task_type)

    def normalize(self, value: Any, task_type: TaskType) -> dict[str, Any]:
        output_type = self.get(task_type)
        if output_type is None:
            return {"text": str(value)}
        if not isinstance(value, output_type):
            value = output_type.model_validate(value)
        return cast(dict[str, Any], value.model_dump(mode="json"))


output_type_registry = TaskOutputTypeRegistry()
