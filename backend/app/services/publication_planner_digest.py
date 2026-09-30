from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from app.models.content import ContentVersion


def build_article_planning_digest(version: ContentVersion, title: str) -> dict[str, Any]:
    """Build a bounded, deterministic, section-balanced digest from one version."""
    blocks = [
        re.sub(r"\s+", " ", block).strip()
        for block in re.split(r"\n\s*\n", version.content)
        if block.strip()
    ]
    sections: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for paragraph_index, paragraph in enumerate(blocks, start=1):
        if _is_non_substantive_heading(paragraph, title):
            if current:
                sections.append(current)
                current = []
            continue
        if len(paragraph) < 40:
            continue
        current.append((paragraph_index, paragraph))
    if current:
        sections.append(current)

    total_candidates = sum(len(section) for section in sections)
    quotas = [1] * len(sections)
    while sum(quotas) < min(24, total_candidates):
        candidate = max(
            range(len(sections)),
            key=lambda index: (
                (len(sections[index]) - quotas[index]) / len(sections[index]),
                len(sections[index]) - quotas[index],
                -index,
            ),
        )
        if quotas[candidate] >= len(sections[candidate]):
            break
        quotas[candidate] += 1

    selected: list[tuple[int, str]] = []
    for section, quota in zip(sections, quotas, strict=False):
        if quota >= len(section):
            selected.extend(section)
            continue
        indices = {
            round(index * (len(section) - 1) / (quota - 1)) if quota > 1 else len(section) // 2
            for index in range(quota)
        }
        selected.extend(section[index] for index in sorted(indices))
    selected.sort(key=lambda value: value[0])

    claims = [
        {
            "claim_id": f"article_{version.id.hex[:8]}_p{paragraph_index:02d}",
            "section": f"paragraph_{paragraph_index}",
            "source_locator": {"paragraph_index": paragraph_index},
            "claim": paragraph[:800],
        }
        for paragraph_index, paragraph in selected
    ]
    claim_texts = [claim["claim"] for claim in claims]
    return {
        "source_content_version_id": str(version.id),
        "title": title,
        "allowed_claims": claims,
        "allowed_management_situations": claim_texts[:8],
        "allowed_distinctions": claim_texts[8:16],
        "allowed_consequences": claim_texts[16:24],
    }


def claim_ids_for_version(version_id: UUID, count: int = 1) -> list[str]:
    return [f"article_{version_id.hex[:8]}_p{index:02d}" for index in range(1, count + 1)]


def _is_non_substantive_heading(value: str, title: str) -> bool:
    normalized = value.casefold().strip()
    if normalized == title.casefold().strip():
        return True
    if re.match(r"^\d+\.\s", value):
        return True
    if normalized in {"заключение", "введение"}:
        return True
    return len(value) < 90 and value[-1:] not in ".!?;:"
