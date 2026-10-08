"""Application-owned immutable evidence identity, independent of analysis context."""

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def evidence_fingerprint(snapshot: dict[str, Any]) -> str:
    identity = {
        "campaign_id": str(snapshot["campaign_id"]),
        "publication_metrics_snapshot_ids": sorted(
            set(
                snapshot.get(
                    "publication_metrics_snapshot_ids", snapshot.get("metrics_snapshot_ids", [])
                )
            )
        ),
        "marketing_feedback_ids": sorted(
            set(snapshot.get("marketing_feedback_ids", snapshot.get("feedback_ids", [])))
        ),
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
