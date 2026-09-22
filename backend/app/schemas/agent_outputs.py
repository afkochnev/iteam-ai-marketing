from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.task import TaskPriority, TaskType


class SocialChannel(StrEnum):
    TELEGRAM = "TELEGRAM"
    VK = "VK"


class RecommendedArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    angle: str = Field(min_length=1)
    cta: str = Field(min_length=1)


class SocialStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: list[SocialChannel] = Field(min_length=1)
    post_count: int = Field(ge=5, le=10)
    approach: str = Field(min_length=1)


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_\-]+$")
    task_type: TaskType
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    agent_slug: str = Field(min_length=1)
    priority: TaskPriority = TaskPriority.NORMAL
    brief: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mapping(self) -> "PlannedTask":
        mapping = {
            TaskType.KNOWLEDGE_RESEARCH: "knowledge_keeper",
            TaskType.WRITE_ARTICLE: "writer",
            TaskType.CREATE_SOCIAL_POSTS: "smm_manager",
        }
        if self.task_type not in mapping or self.agent_slug != mapping[self.task_type]:
            raise ValueError("Недопустимое соответствие типа задачи и агента.")
        return self


class CampaignPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_summary: str = Field(min_length=1)
    positioning: str = Field(min_length=1)
    target_audience: str = Field(min_length=1)
    main_message: str = Field(min_length=1)
    content_strategy: str = Field(min_length=1)
    content_topics: list[str] = Field(min_length=3, max_length=12)
    recommended_article: RecommendedArticle
    social_strategy: SocialStrategy
    tasks: list[PlannedTask] = Field(min_length=3, max_length=15)

    @field_validator("content_topics")
    @classmethod
    def validate_topics(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("Темы контента не могут быть пустыми.")
        return normalized

    @model_validator(mode="after")
    def validate_graph(self) -> "CampaignPlan":
        by_key = {item.key: item for item in self.tasks}
        if len(by_key) != len(self.tasks):
            raise ValueError("Ключи задач должны быть уникальны.")
        for item in self.tasks:
            if item.key in item.depends_on:
                raise ValueError("Задача не может зависеть от себя.")
            if any(key not in by_key for key in item.depends_on):
                raise ValueError("Зависимость ссылается на неизвестную задачу.")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("Граф задач содержит цикл.")
            if key in visited:
                return
            visiting.add(key)
            for dependency in by_key[key].depends_on:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in by_key:
            visit(key)
        types = {item.task_type for item in self.tasks}
        required = {
            TaskType.KNOWLEDGE_RESEARCH,
            TaskType.WRITE_ARTICLE,
            TaskType.CREATE_SOCIAL_POSTS,
        }
        if not required <= types:
            raise ValueError("План должен содержать research, article и social задачи.")

        def has_ancestor(key: str, expected: TaskType) -> bool:
            stack = list(by_key[key].depends_on)
            seen: set[str] = set()
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                if by_key[current].task_type is expected:
                    return True
                stack.extend(by_key[current].depends_on)
            return False

        for item in self.tasks:
            if item.task_type is TaskType.WRITE_ARTICLE and not has_ancestor(
                item.key, TaskType.KNOWLEDGE_RESEARCH
            ):
                raise ValueError("Статья должна зависеть от исследования знаний.")
            if item.task_type is TaskType.CREATE_SOCIAL_POSTS and not has_ancestor(
                item.key, TaskType.WRITE_ARTICLE
            ):
                raise ValueError("Социальные публикации должны зависеть от статьи.")
        return self


class SelectedKnowledgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_key: str = Field(min_length=64, max_length=64)
    selection_reason: str = Field(min_length=1, max_length=5000)

    @field_validator("result_key", "selection_reason")
    @classmethod
    def strip_selected_fields(cls, value: str) -> str:
        return value.strip()


class KnowledgeResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research_query: str = Field(min_length=1, max_length=5000)
    summary: str = Field(min_length=1, max_length=50_000)
    sufficient: bool
    selected_results: list[SelectedKnowledgeResult] = Field(default_factory=list, max_length=12)
    gaps: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("research_query", "summary")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Поле не может быть пустым.")
        return value

    @field_validator("gaps")
    @classmethod
    def normalize_gaps(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("Пробел в знаниях не может быть пустым.")
        return normalized

    @model_validator(mode="after")
    def validate_research_semantics(self) -> "KnowledgeResearchResult":
        keys = [item.result_key for item in self.selected_results]
        if len(keys) != len(set(keys)):
            raise ValueError("Выбранные результаты не должны повторяться.")
        if self.sufficient and not self.selected_results:
            raise ValueError("Достаточный результат должен содержать источник.")
        if not self.sufficient and not self.gaps:
            raise ValueError("При недостатке знаний необходимо описать пробелы.")
        return self
