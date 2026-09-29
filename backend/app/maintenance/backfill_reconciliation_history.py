"""Evidence-only repair for the two historical reconciliation rows.

The command is intentionally allowlisted and dry-run by default.  It never
changes a Publication; it only restores the missing append-only history rows
and records a separate audit event in the same transaction.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.models.activity import ActivityLog
from app.models.publication import (
    Publication,
    PublicationReconciliation,
    PublicationStatus,
    ReconciliationDecision,
)


@dataclass(frozen=True)
class BackfillSpec:
    publication_id: UUID
    decision: ReconciliationDecision
    operator_user_id: UUID
    activity_log_id: UUID
    activity_created_at: datetime
    external_id: str | None
    expected_channel: str
    expected_failure_code: str | None = None
    expected_publication_status: PublicationStatus = PublicationStatus.PUBLISHED


@dataclass(frozen=True)
class BackfillInspection:
    spec: BackfillSpec
    publication: Publication
    activity: ActivityLog
    existing: PublicationReconciliation | None


BACKFILL_SPECS: tuple[BackfillSpec, ...] = (
    BackfillSpec(
        publication_id=UUID("15749868-7f09-479c-9a12-67bca8c73253"),
        decision=ReconciliationDecision.CONFIRMED_NOT_PUBLISHED,
        operator_user_id=UUID("815c9536-bbf7-4b71-9e72-028c4a3698c5"),
        activity_log_id=UUID("00606f12-daef-40f9-9db3-73fdb000f2e6"),
        activity_created_at=datetime.fromisoformat("2026-09-28T08:57:06.581249+00:00"),
        external_id=None,
        expected_channel="VK",
        expected_failure_code="PUBLICATION_RECONCILED_NOT_PUBLISHED",
        expected_publication_status=PublicationStatus.FAILED,
    ),
    BackfillSpec(
        publication_id=UUID("d4b53100-ddc7-4f27-bfaa-9ff3cd59f53a"),
        decision=ReconciliationDecision.CONFIRMED_PUBLISHED,
        operator_user_id=UUID("815c9536-bbf7-4b71-9e72-028c4a3698c5"),
        activity_log_id=UUID("8735c96c-821a-47f1-a750-d45835ee5266"),
        activity_created_at=datetime.fromisoformat("2026-09-28T08:57:12.952077+00:00"),
        external_id="999999",
        expected_channel="TELEGRAM",
    ),
)


class BackfillValidationError(RuntimeError):
    """Raised when durable evidence does not exactly match the allowlist."""


async def inspect_spec(session: AsyncSession, spec: BackfillSpec) -> BackfillInspection:
    publication = await session.get(Publication, spec.publication_id)
    if publication is None:
        raise BackfillValidationError(f"Publication not found: {spec.publication_id}")
    if publication.channel.value != spec.expected_channel:
        raise BackfillValidationError(f"Unexpected channel: {spec.publication_id}")
    if publication.status is not spec.expected_publication_status:
        raise BackfillValidationError(f"Unexpected status: {spec.publication_id}")
    if (
        spec.expected_failure_code is not None
        and publication.failure_code != spec.expected_failure_code
    ):
        raise BackfillValidationError(f"Unexpected failure code: {spec.publication_id}")
    if spec.external_id is not None and publication.external_id != spec.external_id:
        raise BackfillValidationError(f"Unexpected external ID: {spec.publication_id}")

    activity = await session.get(ActivityLog, spec.activity_log_id)
    if activity is None:
        raise BackfillValidationError(f"ActivityLog not found: {spec.activity_log_id}")
    expected_event = (
        "PUBLICATION_RECONCILED_PUBLISHED"
        if spec.decision is ReconciliationDecision.CONFIRMED_PUBLISHED
        else "PUBLICATION_RECONCILED_NOT_PUBLISHED"
    )
    if activity.event_type != expected_event:
        raise BackfillValidationError(f"Unexpected ActivityLog event: {spec.activity_log_id}")
    if activity.user_id != spec.operator_user_id:
        raise BackfillValidationError(f"Operator identity mismatch: {spec.publication_id}")
    if activity.created_at != spec.activity_created_at:
        raise BackfillValidationError(f"Activity timestamp mismatch: {spec.activity_log_id}")
    if activity.metadata_.get("publication_id") != str(spec.publication_id):
        raise BackfillValidationError(f"Activity publication mismatch: {spec.activity_log_id}")
    if spec.external_id is not None and activity.metadata_.get("external_id") != spec.external_id:
        raise BackfillValidationError(f"Activity external ID mismatch: {spec.activity_log_id}")

    existing = await session.scalar(
        select(PublicationReconciliation)
        .where(PublicationReconciliation.publication_id == spec.publication_id)
        .limit(1)
    )
    return BackfillInspection(spec, publication, activity, existing)


async def inspect_all(
    session: AsyncSession, specs: Sequence[BackfillSpec] = BACKFILL_SPECS
) -> list[BackfillInspection]:
    return [await inspect_spec(session, spec) for spec in specs]


async def apply_inspections(
    inspections: Sequence[BackfillInspection], session: AsyncSession
) -> int:
    if any(item.existing is not None for item in inspections):
        raise BackfillValidationError("Refusing to apply: reconciliation history already exists")
    rows: list[PublicationReconciliation] = []
    for item in inspections:
        spec = item.spec
        row = PublicationReconciliation(
            publication_id=spec.publication_id,
            operator_user_id=spec.operator_user_id,
            channel=item.publication.channel,
            decision=spec.decision,
            external_id=spec.external_id,
            external_url=None,
            external_published_at=None,
            note=None,
            created_at=spec.activity_created_at,
            updated_at=spec.activity_created_at,
        )
        rows.append(row)
        session.add(row)
    await session.flush()
    for item, row in zip(inspections, rows, strict=True):
        spec = item.spec
        session.add(
            ActivityLog(
                event_type="PUBLICATION_RECONCILIATION_HISTORY_BACKFILLED",
                operation_key=f"reconciliation-history-backfill:{spec.publication_id}",
                campaign_id=item.publication.campaign_id,
                user_id=spec.operator_user_id,
                content_item_id=item.publication.content_item_id,
                metadata_={
                    "publication_id": str(spec.publication_id),
                    "reconciliation_id": str(row.id),
                    "original_activity_log_id": str(spec.activity_log_id),
                    "original_activity_log_timestamp": spec.activity_created_at.isoformat(),
                    "decision": spec.decision.value,
                    "backfill_reason": "historical_reconciliation_history_repair",
                },
            )
        )
    await session.commit()
    return len(inspections)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply the verified repair")
    parser.add_argument(
        "--publication-id",
        action="append",
        choices=[str(spec.publication_id) for spec in BACKFILL_SPECS],
        help="restrict the operation to one allowlisted Publication",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    selected = set(args.publication_id or [str(spec.publication_id) for spec in BACKFILL_SPECS])
    specs = [spec for spec in BACKFILL_SPECS if str(spec.publication_id) in selected]
    async with async_session_factory() as session:
        inspections = await inspect_all(session, specs)
        for item in inspections:
            spec = item.spec
            if item.existing is not None:
                print(f"ALREADY_PRESENT publication={spec.publication_id} row={item.existing.id}")
            else:
                print(
                    f"WOULD_INSERT publication={spec.publication_id} "
                    f"decision={spec.decision.value} operator={spec.operator_user_id} "
                    f"created_at={spec.activity_created_at.isoformat()} "
                    f"external_id={spec.external_id!r}"
                )
        if not args.apply:
            print("DRY_RUN_ONLY no mutations performed")
            return 0
        count = await apply_inspections(inspections, session)
        print(f"APPLIED rows={count}")
        return 0


def main() -> None:
    args = _parser().parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except BackfillValidationError as error:
        raise SystemExit(f"REFUSED: {error}") from error


if __name__ == "__main__":
    main()
