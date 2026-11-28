import json
from typing import Any

import httpx
import pytest

from app.integrations.studio_wordpress import (
    WordPressArticle,
    WordPressError,
    WordPressPost,
    WordPressStudioClient,
    WordPressTarget,
)

TARGET = WordPressTarget("https://staging.example.test", 29)
POST_ID = 17
POST_URL = f"{TARGET.url}/first-slug/"
POST_PATH = f"/wp-json/wp/v2/posts/{POST_ID}"
PREVIOUS = WordPressArticle("first-slug", "First title", "<p>First body</p>")
REPLACEMENT = WordPressArticle("first-slug", "Second title", "<p>Second body</p>")


def publication(article: WordPressArticle, media_id: int) -> dict[str, Any]:
    return {
        "id": POST_ID,
        "type": "post",
        "status": "publish",
        "slug": article.slug,
        "author": TARGET.author_id,
        "title": {"raw": article.title},
        "content": {"raw": article.html},
        "featured_media": media_id,
        "link": POST_URL,
    }


async def test_replace_and_restore_keep_post_identity() -> None:
    result = publication(PREVIOUS, 41)
    methods = []

    def wordpress(request: httpx.Request) -> httpx.Response:
        assert request.url.path == POST_PATH
        methods.append(request.method)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert set(payload) == {"title", "content", "featured_media"}
            result.update(
                title={"raw": payload["title"]},
                content={"raw": payload["content"]},
                featured_media=payload["featured_media"],
            )
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wordpress)) as client:
        adapter = WordPressStudioClient(client, TARGET, "editor", "password")
        receipt = await adapter.replace_publication(PREVIOUS, REPLACEMENT, POST_ID, 41, 0, POST_URL)
        assert receipt == WordPressPost(POST_ID, POST_URL)
        assert result == publication(REPLACEMENT, 0)
        assert adapter.write_attempted
        receipt = await adapter.replace_publication(REPLACEMENT, PREVIOUS, POST_ID, 0, 41, POST_URL)
        assert receipt == WordPressPost(POST_ID, POST_URL)
        assert result == publication(PREVIOUS, 41)
    assert methods == ["GET", "POST", "GET", "POST"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 18),
        ("type", "page"),
        ("status", "draft"),
        ("author", 30),
        ("slug", "another-slug"),
        ("link", f"{TARGET.url}/another-slug/"),
        ("title", {"raw": "Edited by hand"}),
        ("content", {"raw": "<p>Edited by hand</p>"}),
        ("featured_media", 42),
    ],
)
async def test_replacement_does_not_write_when_remote_publication_differs(
    field: str, value: object
) -> None:
    def wordpress(request: httpx.Request) -> httpx.Response:
        assert request.url.path == POST_PATH
        assert request.method == "GET"
        result = publication(PREVIOUS, 41)
        result[field] = value
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wordpress)) as client:
        adapter = WordPressStudioClient(client, TARGET, "editor", "password")
        with pytest.raises(WordPressError, match="WORDPRESS_(RESPONSE|URL)_MISMATCH"):
            await adapter.replace_publication(PREVIOUS, REPLACEMENT, POST_ID, 41, 0, POST_URL)
        assert not adapter.write_attempted


async def test_replacement_verifies_response_and_reconciles_by_reading() -> None:
    result = publication(PREVIOUS, 41)

    def wordpress(request: httpx.Request) -> httpx.Response:
        assert request.url.path == POST_PATH
        if request.method == "POST":
            result.update(publication(REPLACEMENT, 0))
            return httpx.Response(200, json={**result, "link": f"{TARGET.url}/changed/"})
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wordpress)) as client:
        adapter = WordPressStudioClient(client, TARGET, "editor", "password")
        assert (
            await adapter.find_replacement(PREVIOUS, REPLACEMENT, POST_ID, 41, 0, POST_URL) is None
        )
        assert not adapter.write_attempted
        with pytest.raises(WordPressError, match="WORDPRESS_URL_MISMATCH"):
            await adapter.replace_publication(PREVIOUS, REPLACEMENT, POST_ID, 41, 0, POST_URL)
        assert adapter.write_attempted
        adapter.write_attempted = False
        receipt = await adapter.find_replacement(PREVIOUS, REPLACEMENT, POST_ID, 41, 0, POST_URL)
        assert receipt == WordPressPost(POST_ID, POST_URL)
        result["featured_media"] = 42
        with pytest.raises(WordPressError, match="WORDPRESS_RESPONSE_MISMATCH"):
            await adapter.find_replacement(PREVIOUS, REPLACEMENT, POST_ID, 41, 0, POST_URL)
        assert not adapter.write_attempted
