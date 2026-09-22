from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SECRET_KEYS = {"authorization", "password", "openai_api_key", "jwt_secret", "secret", "token"}


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_secret_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str) and (value.startswith("postgres") or value.startswith("redis")):
        return _redact_url(value)
    return value


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(("_password", "_token", "_secret"))


def _redact_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.password:
        return value
    user = parts.username or ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{user}:[REDACTED]@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
