from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.agents.output_registry import output_type_registry
from app.models.task import TaskType
from app.schemas.agent_outputs import SocialPostPackResult


def _post(
    index: int, channel: str = "TELEGRAM", *, version: UUID | None = None, section: str = "problem"
) -> dict[str, object]:
    return {
        "key": f"post_{index}",
        "channel": channel,
        "title": f"Пост {index}",
        "text_markdown": "Проверенный текст поста.",
        "cta": "Узнать больше",
        "sources": [{"content_version_id": str(version or uuid4()), "section_key": section}],
        "suggested_publish_order": index,
    }


def _valid(**changes: object) -> dict[str, object]:
    payload = {
        "sufficient": True,
        "pack": {"strategy_summary": "Серия постов", "posts": [_post(i) for i in range(1, 6)]},
    }
    payload.update(changes)
    return payload


def test_social_pack_schema_and_registry() -> None:
    result = SocialPostPackResult.model_validate(_valid())
    assert result.sufficient
    assert output_type_registry.get(TaskType.CREATE_SOCIAL_POSTS) is SocialPostPackResult


def test_social_pack_schema_accepts_global_order_for_nine_posts() -> None:
    posts = [_post(index, "TELEGRAM" if index % 2 else "VK") for index in range(1, 10)]
    result = SocialPostPackResult.model_validate(
        {"sufficient": True, "pack": {"strategy_summary": "x", "posts": posts}}
    )
    assert [post.suggested_publish_order for post in result.pack.posts] == list(range(1, 10))


@pytest.mark.parametrize(
    "payload",
    [
        {"sufficient": True, "pack": None},
        {"sufficient": False, "pack": _valid()["pack"], "gaps": ["gap"]},
        {"sufficient": False, "pack": None, "gaps": []},
        {
            "sufficient": True,
            "pack": {"strategy_summary": "x", "posts": [_post(i) for i in range(1, 5)]},
        },
        {
            "sufficient": True,
            "pack": {"strategy_summary": "x", "posts": [_post(i) for i in range(1, 12)]},
        },
    ],
)
def test_social_pack_semantics_reject_invalid(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SocialPostPackResult.model_validate(payload)


def test_social_pack_rejects_duplicate_keys_and_orders() -> None:
    posts = [_post(i) for i in range(1, 6)]
    posts[1]["key"] = posts[0]["key"]
    with pytest.raises(ValidationError):
        SocialPostPackResult.model_validate(
            {"sufficient": True, "pack": {"strategy_summary": "x", "posts": posts}}
        )
    posts = [_post(i) for i in range(1, 6)]
    posts[1]["suggested_publish_order"] = 1
    with pytest.raises(ValidationError):
        SocialPostPackResult.model_validate(
            {"sufficient": True, "pack": {"strategy_summary": "x", "posts": posts}}
        )


def test_social_pack_rejects_invalid_channel_and_empty_sources() -> None:
    posts = [_post(i) for i in range(1, 6)]
    posts[0]["channel"] = "INSTAGRAM"
    with pytest.raises(ValidationError):
        SocialPostPackResult.model_validate(
            {"sufficient": True, "pack": {"strategy_summary": "x", "posts": posts}}
        )


@pytest.mark.parametrize("bad_text", ["**важно**", "# Заголовок\nТекст", "```json\n{}\n```"])
def test_social_pack_rejects_publishable_format_artifacts(bad_text: str) -> None:
    posts = [_post(i) for i in range(1, 6)]
    posts[0]["text_markdown"] = bad_text
    with pytest.raises(ValidationError, match="Недопустимое форматирование"):
        SocialPostPackResult.model_validate(
            {"sufficient": True, "pack": {"strategy_summary": "x", "posts": posts}}
        )


def test_social_pack_allows_optional_cta_but_keeps_plain_text_contract() -> None:
    posts = [_post(i) for i in range(1, 6)]
    posts[0]["cta"] = ""
    result = SocialPostPackResult.model_validate(
        {"sufficient": True, "pack": {"strategy_summary": "x", "posts": posts}}
    )
    assert result.pack is not None
    assert result.pack.posts[0].cta == ""
    posts = [_post(i) for i in range(1, 6)]
    posts[0]["sources"] = []
    with pytest.raises(ValidationError):
        SocialPostPackResult.model_validate(
            {"sufficient": True, "pack": {"strategy_summary": "x", "posts": posts}}
        )
