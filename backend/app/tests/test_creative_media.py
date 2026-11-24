import asyncio
import json
from base64 import b64encode
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image

from app.integrations import creative_media


def image_data(image_format: str = "PNG") -> bytes:
    with Image.new("RGB", (120, 80), (25, 40, 60)) as image, BytesIO() as output:
        image.save(output, format=image_format)
        return output.getvalue()


@pytest.fixture
def media_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(creative_media.settings, "creative_media_root", tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("format_name", "mime_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
async def test_store_exact_original_and_channel_variants(
    media_dir: Path, format_name: str, mime_type: str
) -> None:
    data = image_data(format_name)
    stored = await creative_media.store_creative_image(data)

    assert stored.original == creative_media.CreativeImageObject(
        sha256(data).hexdigest(), mime_type, len(data), 120, 80
    )
    assert await creative_media.read_creative_image(stored.original.digest) == data
    assert {item.name: (item.image.width, item.image.height) for item in stored.variants} == {
        "website": (1200, 630),
        "vk": (1080, 1080),
        "email": (1200, 628),
        "presentation": (1920, 1080),
    }
    for item in stored.variants:
        assert item.image.mime_type == "image/webp"
        variant = await creative_media.read_creative_image(item.image.digest)
        assert sha256(variant).hexdigest() == item.image.digest
        assert len(variant) == item.image.byte_size
        with Image.open(BytesIO(variant)) as decoded:
            decoded.load()
            assert decoded.format == "WEBP"
            assert decoded.size == (item.image.width, item.image.height)


async def test_same_image_is_reusable_during_concurrent_writes(media_dir: Path) -> None:
    data = image_data()
    first, second = await asyncio.gather(
        creative_media.store_creative_image(data), creative_media.store_creative_image(data)
    )
    assert first == second
    assert sum(path.is_file() for path in media_dir.glob("*/*")) == 5
    assert await creative_media.read_creative_image(first.original.digest) == data


async def test_invalid_image_and_animated_webp_are_rejected_before_write(media_dir: Path) -> None:
    with pytest.raises(creative_media.CreativeImageValidationError, match="FORMAT_INVALID"):
        await creative_media.store_creative_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    with pytest.raises(creative_media.CreativeImageValidationError, match="FORMAT_INVALID"):
        await creative_media.store_creative_image(image_data("JPEG")[:-20])

    with (
        Image.new("RGB", (10, 10), "black") as first,
        Image.new("RGB", (10, 10), "white") as second,
        BytesIO() as output,
    ):
        first.save(output, format="WEBP", save_all=True, append_images=[second], duration=100)
        with pytest.raises(creative_media.CreativeImageValidationError, match="ANIMATED"):
            await creative_media.store_creative_image(output.getvalue())
    assert not any(media_dir.iterdir())


async def test_size_limit_is_applied_before_decode(
    media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(creative_media, "MAX_CREATIVE_IMAGE_BYTES", 64)
    with pytest.raises(creative_media.CreativeImageValidationError, match="SIZE_INVALID"):
        await creative_media.store_creative_image(image_data())
    assert not any(media_dir.iterdir())


async def test_read_rejects_non_canonical_keys_and_corrupted_objects(media_dir: Path) -> None:
    with pytest.raises(creative_media.CreativeMediaNotFoundError):
        await creative_media.read_creative_image("../credentials")
    with pytest.raises(creative_media.CreativeMediaNotFoundError):
        await creative_media.read_creative_image("f" * 64)

    stored = await creative_media.store_creative_image(image_data())
    digest = stored.original.digest
    (media_dir / digest[:2] / digest).write_bytes(b"corrupt")
    with pytest.raises(creative_media.CreativeMediaStorageError, match="INTEGRITY_FAILED"):
        await creative_media.read_creative_image(digest)
    with pytest.raises(creative_media.CreativeMediaStorageError, match="INTEGRITY_FAILED"):
        await creative_media.store_creative_image(image_data())


def mock_image_api(
    monkeypatch: pytest.MonkeyPatch,
    *responses: httpx.Response | Exception,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    pending = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        calls.append(json.loads(request.content))
        response = next(pending)
        if isinstance(response, Exception):
            raise response
        return response

    def client(**kwargs: Any) -> AsyncOpenAI:
        assert kwargs["max_retries"] == 0
        assert kwargs["timeout"] == 120
        return AsyncOpenAI(
            base_url="https://creative.test/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            **kwargs,
        )

    monkeypatch.setattr(creative_media.settings, "openai_api_key", "test-only")
    monkeypatch.setattr(creative_media.settings, "creative_image_model", "test-image-model")
    monkeypatch.setattr(creative_media.settings, "creative_image_timeout_seconds", 120)
    monkeypatch.setattr(creative_media, "AsyncOpenAI", client)
    return calls


async def test_generate_exactly_one_image_with_a_bounded_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = image_data()
    calls = mock_image_api(
        monkeypatch,
        httpx.Response(
            200,
            headers={"x-request-id": "test-response"},
            json={"created": 1, "data": [{"b64_json": b64encode(image).decode()}]},
        ),
    )
    result = await creative_media.generate_creative_image(
        prompt="Exact brief prompt", model="test-image-model", size="1024x1024"
    )
    assert result.content == image
    assert result.provider_request_id == "test-response"
    assert calls == [
        {
            "prompt": "Exact brief prompt",
            "model": "test-image-model",
            "size": "1024x1024",
            "n": 1,
            "output_format": "png",
        }
    ]


@pytest.mark.parametrize(
    ("upstream", "code", "outcome_unknown"),
    [
        (httpx.ReadTimeout("test-only timeout"), "CREATIVE_GENERATION_OUTCOME_UNKNOWN", True),
        (httpx.Response(503), "CREATIVE_GENERATION_OUTCOME_UNKNOWN", True),
        (httpx.Response(429), "CREATIVE_GENERATION_REJECTED", False),
        (
            httpx.Response(200, json={"created": 1, "data": [{"b64_json": "not-base64"}]}),
            "CREATIVE_GENERATION_RESPONSE_INVALID",
            True,
        ),
    ],
)
async def test_generate_failure_classification_preserves_one_call(
    monkeypatch: pytest.MonkeyPatch,
    upstream: httpx.Response | Exception,
    code: str,
    outcome_unknown: bool,
) -> None:
    calls = mock_image_api(monkeypatch, upstream)
    with pytest.raises(creative_media.CreativeImageGenerationError) as error:
        await creative_media.generate_creative_image(
            prompt="Exact brief prompt", model="test-image-model", size="1024x1024"
        )
    assert error.value.code == code
    assert error.value.outcome_unknown is outcome_unknown
    assert len(calls) == 1


async def test_disabled_or_replaced_model_never_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = mock_image_api(monkeypatch)
    with pytest.raises(creative_media.CreativeImageGenerationError, match="NOT_CONFIGURED"):
        await creative_media.generate_creative_image(
            prompt="Exact brief prompt", model="old-image-model", size="1024x1024"
        )
    assert not calls
