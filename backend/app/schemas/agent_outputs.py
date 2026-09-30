from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.task import TaskPriority, TaskType
from app.services.social_content_quality import social_text_quality_errors


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


class ArticleSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    heading: str = Field(min_length=1, max_length=255)
    body_markdown: str = Field(min_length=1, max_length=100_000)
    knowledge_pack_item_ids: list[UUID] = Field(default_factory=list)

    @field_validator("key", "heading", "body_markdown")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Поле не может быть пустым.")
        return value

    @field_validator("knowledge_pack_item_ids")
    @classmethod
    def unique_sources(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Источники раздела не должны повторяться.")
        return value


class ArticleDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    subtitle: str | None = None
    lead: str = Field(min_length=1, max_length=50_000)
    sections: list[ArticleSection] = Field(min_length=3, max_length=12)
    conclusion: str = Field(min_length=1, max_length=50_000)
    cta: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def unique_section_keys(self) -> "ArticleDraft":
        keys = [section.key for section in self.sections]
        if len(keys) != len(set(keys)):
            raise ValueError("Ключи разделов должны быть уникальны.")
        return self


class ArticleWritingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sufficient: bool
    article: ArticleDraft | None = None
    gaps: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_semantics(self) -> "ArticleWritingResult":
        if self.sufficient and self.article is None:
            raise ValueError("Достаточный результат должен содержать статью.")
        if not self.sufficient and self.article is not None:
            raise ValueError("Недостаточный результат не должен содержать статью.")
        if not self.sufficient and not self.gaps:
            raise ValueError("При недостатке материалов необходимо указать пробелы.")
        return self


class SocialPostSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_version_id: UUID
    section_key: str = Field(min_length=1, max_length=100)


class SocialPostBaseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    text_markdown: str = Field(min_length=1, max_length=20_000)
    cta: str = Field(default="", max_length=5_000)
    sources: list[SocialPostSourceRef] = Field(min_length=1)
    suggested_publish_order: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_plain_text_contract(self) -> "SocialPostBaseDraft":
        errors = social_text_quality_errors(self.text_markdown)
        if self.cta:
            errors.extend(social_text_quality_errors(self.cta))
        if errors:
            raise ValueError("Недопустимое форматирование Social Post: " + ", ".join(errors))
        return self


class SocialPostDraft(SocialPostBaseDraft):
    channel: str


class PlanSocialPostDraft(SocialPostBaseDraft):
    """Plan-bound post content; channel is owned by PublicationPlanItem."""


class SocialPostPackDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_summary: str = Field(min_length=1, max_length=20_000)
    posts: list[SocialPostDraft] = Field(min_length=5, max_length=10)

    @model_validator(mode="after")
    def validate_posts(self) -> "SocialPostPackDraft":
        keys = [post.key for post in self.posts]
        orders = [post.suggested_publish_order for post in self.posts]
        if len(keys) != len(set(keys)):
            raise ValueError("Ключи постов должны быть уникальны.")
        if len(orders) != len(set(orders)) or set(orders) != set(range(1, len(self.posts) + 1)):
            raise ValueError("Порядок публикации должен быть уникальным и последовательным.")
        if any(post.channel not in {"TELEGRAM", "VK"} for post in self.posts):
            raise ValueError("Недопустимый канал публикации.")
        return self


class SocialPostPackResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sufficient: bool
    pack: SocialPostPackDraft | None = None
    gaps: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_semantics(self) -> "SocialPostPackResult":
        if self.sufficient and self.pack is None:
            raise ValueError("Достаточный результат должен содержать пакет публикаций.")
        if not self.sufficient and self.pack is not None:
            raise ValueError("Недостаточный результат не должен содержать пакет публикаций.")
        if not self.sufficient and not self.gaps:
            raise ValueError("При недостатке материалов необходимо указать пробелы.")
        return self


class SingleSocialPostPackDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_summary: str = Field(min_length=1, max_length=20_000)
    posts: list[PlanSocialPostDraft] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def validate_single_post(self) -> "SingleSocialPostPackDraft":
        post = self.posts[0]
        if post.suggested_publish_order != 1:
            raise ValueError("Для plan item порядок поста должен быть равен 1.")
        return self


class SingleSocialPostResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sufficient: bool
    pack: SingleSocialPostPackDraft | None = None
    gaps: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_semantics(self) -> "SingleSocialPostResult":
        if self.sufficient and self.pack is None:
            raise ValueError("Достаточный результат должен содержать пост.")
        if not self.sufficient and self.pack is not None:
            raise ValueError("Недостаточный результат не должен содержать пост.")
        if not self.sufficient and not self.gaps:
            raise ValueError("При недостатке материалов необходимо указать пробелы.")
        return self
