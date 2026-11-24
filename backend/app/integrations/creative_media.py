"""Private creative asset storage and the single-image generation adapter."""

import asyncio
import os
import re
from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore
from typing import Literal

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, OpenAIError
from PIL import Image, ImageOps

from app.core.config import settings
from app.core.runtime_admission import RuntimeAdmissionError, require_active_runtime

MAX_CREATIVE_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CREATIVE_IMAGE_PIXELS = 20_000_000
MAX_CREATIVE_IMAGE_PROMPT_CHARS = 16_000
_MAX_ENCODED_IMAGE_LENGTH = 4 * ((MAX_CREATIVE_IMAGE_BYTES + 2) // 3)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_VARIANT_SIZES = (
    ("website", 1200, 630),
    ("vk", 1080, 1080),
    ("email", 1200, 628),
    ("presentation", 1920, 1080),
)
_IMAGE_PROCESSING_SLOTS = BoundedSemaphore(2)

CreativeImageSize = Literal["1024x1024", "1536x1024", "1024x1536"]


@dataclass(frozen=True)
class CreativeImageObject:
    digest: str
    mime_type: str
    byte_size: int
    width: int
    height: int


@dataclass(frozen=True)
class CreativeImageVariant:
    name: str
    image: CreativeImageObject


@dataclass(frozen=True)
class StoredCreativeImage:
    original: CreativeImageObject
    variants: tuple[CreativeImageVariant, ...]


@dataclass(frozen=True)
class GeneratedCreativeImage:
    content: bytes
    provider_request_id: str | None


class CreativeImageValidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CreativeMediaStorageError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CreativeMediaNotFoundError(CreativeMediaStorageError):
    def __init__(self) -> None:
        super().__init__("CREATIVE_MEDIA_NOT_FOUND")


class CreativeImageGenerationError(RuntimeError):
    def __init__(self, code: str, *, outcome_unknown: bool) -> None:
        self.code = code
        self.outcome_unknown = outcome_unknown
        super().__init__(code)


def _storage_root() -> Path:
    if settings.creative_media_root is None:
        raise CreativeMediaStorageError("CREATIVE_MEDIA_NOT_CONFIGURED")
    return settings.creative_media_root


def _blob_path(root: Path, digest: str) -> Path:
    if _SHA256.fullmatch(digest) is None:
        raise CreativeMediaNotFoundError()
    return root / digest[:2] / digest


def _read_blob(root: Path, digest: str) -> bytes:
    path = _blob_path(root, digest)
    try:
        with path.open("rb") as file:
            data = file.read(MAX_CREATIVE_IMAGE_BYTES + 1)
    except FileNotFoundError as exc:
        raise CreativeMediaNotFoundError() from exc
    except OSError as exc:
        raise CreativeMediaStorageError("CREATIVE_MEDIA_READ_FAILED") from exc
    if len(data) > MAX_CREATIVE_IMAGE_BYTES or sha256(data).hexdigest() != digest:
        raise CreativeMediaStorageError("CREATIVE_MEDIA_INTEGRITY_FAILED")
    return data


def _write_blob(
    root: Path, data: bytes, mime_type: str, size: tuple[int, int]
) -> CreativeImageObject:
    digest = sha256(data).hexdigest()
    path = _blob_path(root, digest)
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            # The link becomes visible only after the file is complete, and can never replace
            # an existing object. All instances write into the same private filesystem.
            os.link(temporary_path, path)
        except FileExistsError:
            _read_blob(root, digest)
    except OSError as exc:
        raise CreativeMediaStorageError("CREATIVE_MEDIA_WRITE_FAILED") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # A staging name is never a readable object key.
                pass
    return CreativeImageObject(digest, mime_type, len(data), *size)


def _decode_image(data: bytes) -> tuple[Image.Image, str]:
    if not data or len(data) > MAX_CREATIVE_IMAGE_BYTES:
        raise CreativeImageValidationError("CREATIVE_IMAGE_SIZE_INVALID")

    try:
        with Image.open(BytesIO(data), formats=list(_MIME_TYPES)) as candidate:
            mime_type = _MIME_TYPES.get(candidate.format or "")
            if mime_type is None:
                raise CreativeImageValidationError("CREATIVE_IMAGE_FORMAT_INVALID")
            if max(candidate.size) > 10_000 or candidate.width * candidate.height > (
                MAX_CREATIVE_IMAGE_PIXELS
            ):
                raise CreativeImageValidationError("CREATIVE_IMAGE_DIMENSIONS_INVALID")
            if getattr(candidate, "n_frames", 1) != 1:
                raise CreativeImageValidationError("CREATIVE_IMAGE_ANIMATED")
            candidate.verify()

        with Image.open(BytesIO(data), formats=list(_MIME_TYPES)) as source:
            source.load()
            ImageOps.exif_transpose(source, in_place=True)
            has_alpha = "A" in source.getbands() or "transparency" in source.info
            return source.convert("RGBA" if has_alpha else "RGB"), mime_type
    except CreativeImageValidationError:
        raise
    except Image.DecompressionBombError as exc:
        raise CreativeImageValidationError("CREATIVE_IMAGE_DIMENSIONS_INVALID") from exc
    except (OSError, SyntaxError, ValueError, TypeError) as exc:
        raise CreativeImageValidationError("CREATIVE_IMAGE_FORMAT_INVALID") from exc


def _store_image(root: Path, data: bytes) -> StoredCreativeImage:
    with _IMAGE_PROCESSING_SLOTS:
        source, mime_type = _decode_image(data)
        with source:
            variants: list[CreativeImageVariant] = []
            for name, width, height in _VARIANT_SIZES:
                try:
                    with (
                        BytesIO() as output,
                        ImageOps.fit(source, (width, height), Image.Resampling.LANCZOS) as variant,
                    ):
                        variant.save(output, format="WEBP", quality=86)
                        encoded = output.getvalue()
                except (OSError, ValueError) as exc:
                    raise CreativeMediaStorageError("CREATIVE_IMAGE_VARIANT_FAILED") from exc
                if len(encoded) > MAX_CREATIVE_IMAGE_BYTES:
                    raise CreativeMediaStorageError("CREATIVE_IMAGE_VARIANT_FAILED")
                stored = _write_blob(root, encoded, "image/webp", (width, height))
                variants.append(CreativeImageVariant(name, stored))

            original = _write_blob(root, data, mime_type, source.size)
    return StoredCreativeImage(original, tuple(variants))


async def store_creative_image(data: bytes) -> StoredCreativeImage:
    return await asyncio.to_thread(_store_image, _storage_root(), data)


async def read_creative_image(digest: str) -> bytes:
    return await asyncio.to_thread(_read_blob, _storage_root(), digest)


async def generate_creative_image(
    *, prompt: str, model: str, size: CreativeImageSize
) -> GeneratedCreativeImage:
    try:
        require_active_runtime()
    except RuntimeAdmissionError as exc:
        raise CreativeImageGenerationError(
            "CREATIVE_GENERATION_RUNTIME_PASSIVE", outcome_unknown=False
        ) from exc
    if not model or settings.creative_image_model != model or not settings.openai_api_key:
        raise CreativeImageGenerationError(
            "CREATIVE_GENERATION_NOT_CONFIGURED", outcome_unknown=False
        )
    if not prompt.strip() or len(prompt) > MAX_CREATIVE_IMAGE_PROMPT_CHARS:
        raise CreativeImageGenerationError(
            "CREATIVE_GENERATION_PROMPT_INVALID", outcome_unknown=False
        )

    timeout = settings.creative_image_timeout_seconds
    try:
        async with (
            AsyncOpenAI(api_key=settings.openai_api_key, timeout=timeout, max_retries=0) as client,
            asyncio.timeout(timeout),
        ):
            response = await client.images.generate(
                model=model, prompt=prompt, size=size, n=1, output_format="png"
            )
    except (APIConnectionError, TimeoutError) as exc:
        raise CreativeImageGenerationError(
            "CREATIVE_GENERATION_OUTCOME_UNKNOWN", outcome_unknown=True
        ) from exc
    except APIStatusError as exc:
        unknown = exc.status_code in {408, 409} or exc.status_code >= 500
        code = "CREATIVE_GENERATION_OUTCOME_UNKNOWN" if unknown else "CREATIVE_GENERATION_REJECTED"
        raise CreativeImageGenerationError(code, outcome_unknown=unknown) from exc
    except OpenAIError as exc:
        raise CreativeImageGenerationError(
            "CREATIVE_GENERATION_OUTCOME_UNKNOWN", outcome_unknown=True
        ) from exc

    items = response.data or []
    payload = items[0].b64_json if len(items) == 1 else None
    if not isinstance(payload, str) or not payload or len(payload) > _MAX_ENCODED_IMAGE_LENGTH:
        raise CreativeImageGenerationError(
            "CREATIVE_GENERATION_RESPONSE_INVALID", outcome_unknown=True
        )
    try:
        content = b64decode(payload, validate=True)
    except (Base64Error, ValueError) as exc:
        raise CreativeImageGenerationError(
            "CREATIVE_GENERATION_RESPONSE_INVALID", outcome_unknown=True
        ) from exc
    if len(content) > MAX_CREATIVE_IMAGE_BYTES:
        raise CreativeImageGenerationError(
            "CREATIVE_GENERATION_RESPONSE_INVALID", outcome_unknown=True
        )
    return GeneratedCreativeImage(content, response._request_id)
