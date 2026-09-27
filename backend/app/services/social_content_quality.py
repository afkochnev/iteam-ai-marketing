"""Objective checks for publishable Social Post text."""

from __future__ import annotations

import re

_FORBIDDEN_LABELS = (
    "cta:",
    "порядок:",
    "основано на разделах статьи:",
    "section_key",
    "content_version_id",
    "provenance",
    "debug metadata",
)
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s")


def social_text_quality_errors(text: str) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        errors.append("empty_text")
    if "**" in text:
        errors.append("markdown_bold")
    if "```" in text:
        errors.append("markdown_fence")
    if _HEADING_RE.search(text):
        errors.append("markdown_heading")
    lowered = text.casefold()
    for label in _FORBIDDEN_LABELS:
        if label in lowered:
            errors.append(f"internal_label:{label}")
    return errors


def social_text_quality_message(errors: list[str]) -> str:
    labels = {
        "empty_text": "текст не должен быть пустым",
        "markdown_bold": "уберите Markdown-жирный текст (**...**)",
        "markdown_fence": "уберите Markdown code fence",
        "markdown_heading": "уберите Markdown-заголовки",
    }
    rendered = [labels.get(error, "уберите внутренние метки и metadata") for error in errors]
    return "Публикация содержит недопустимое форматирование: " + "; ".join(dict.fromkeys(rendered))
