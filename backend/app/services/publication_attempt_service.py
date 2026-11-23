"""Durable evidence for legacy claim-backed provider invocations."""

import hashlib
from uuid import UUID

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.errors import AppError
from app.integrations.telegram import ProviderPublicationResult, PublicationProviderError
from app.models.content import ContentChannel
from app.models.publication import LegacyPublicationAttempt, Publication, PublicationClaimEvent

# Only these adapter classifications establish rejection or a definite pre-send failure.
_DEFINITE_ERRORS = {
    "TELEGRAM_AUTH_ERROR",
    "TELEGRAM_PERMISSION_ERROR",
    "TELEGRAM_RATE_LIMIT",
    "TELEGRAM_BAD_REQUEST",
    "VK_AUTH_ERROR",
    "VK_PERMISSION_ERROR",
    "VK_RATE_LIMIT",
    "VK_BAD_REQUEST",
    "VK_TARGET_INVALID",
    "VK_TARGET_MISMATCH",
    "VK_CREDENTIALS_INVALID",
    "VK_CREDENTIALS_MISSING",
    "VK_CREDENTIALS_PERMISSIONS",
    "VK_CREDENTIALS_IO_ERROR",
    "VK_CREDENTIALS_APP_MISMATCH",
    "VK_CREDENTIALS_USER_MISMATCH",
    "VK_CREDENTIALS_TARGET_MISMATCH",
    "VK_CREDENTIALS_SCOPE_MISSING",
    "VK_AUTH_REFRESH_TIMEOUT",
    "VK_AUTH_REFRESH_ERROR",
}


def safe_failure_code(channel: ContentChannel, error: PublicationProviderError) -> str:
    prefix = channel.value
    if not error.ambiguous and error.code.startswith(prefix + "_"):
        if error.code in _DEFINITE_ERRORS:
            return error.code
        if error.code in {f"{prefix}_PROVIDER_ERROR", f"{prefix}_PROVIDER_TIMEOUT"} and isinstance(
            error.__cause__, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout
        ):
            return error.code
    return f"{prefix}_RECONCILIATION_REQUIRED"


def validate_receipt(receipt: ProviderPublicationResult) -> None:
    def valid_id(value: str) -> bool:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 255

    if not valid_id(receipt.external_id) or (
        receipt.provider_target_id is not None and not valid_id(receipt.provider_target_id)
    ):
        raise ValueError("Incomplete provider receipt")


class PublicationAttemptService:
    """All transitions participate in the caller's locked publication transaction."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _event(publication_id: UUID, token: str) -> Select[tuple[PublicationClaimEvent]]:
        return select(PublicationClaimEvent).where(
            PublicationClaimEvent.publication_id == publication_id,
            PublicationClaimEvent.claim_digest == hashlib.sha256(token.encode()).hexdigest(),
        )

    async def start(self, publication: Publication, token: str) -> bool:
        event: PublicationClaimEvent | None = await self.session.scalar(
            self._event(publication.id, token).execution_options(populate_existing=True)
        )
        if event is None:
            # Pre-0031 claims keep their original execution/audit path.
            return True
        if (
            event.channel != publication.channel
            or event.content_version_id != publication.content_version_id
        ):
            raise AppError(
                "PUBLICATION_CLAIM_SNAPSHOT_MISMATCH",
                "Версия или канал публикации не совпадает с разрешением на отправку.",
                409,
            )
        inserted = await self.session.scalar(
            insert(LegacyPublicationAttempt)
            .values(
                claim_event_id=event.id,
                claimed_target_id=publication.provider_target_id,
                status="STARTED",
            )
            .on_conflict_do_nothing(index_elements=[LegacyPublicationAttempt.claim_event_id])
            .returning(LegacyPublicationAttempt.claim_event_id)
        )
        return inserted is not None

    async def finish(
        self, publication_id: UUID, token: str, outcome: ProviderPublicationResult | str
    ) -> None:
        event_id = (
            self._event(publication_id, token)
            .with_only_columns(PublicationClaimEvent.id)
            .scalar_subquery()
        )
        statement = (
            update(LegacyPublicationAttempt)
            .where(
                LegacyPublicationAttempt.claim_event_id == event_id,
                LegacyPublicationAttempt.status == "STARTED",
            )
            .execution_options(synchronize_session=False)
        )
        if isinstance(outcome, str):
            status = "UNKNOWN" if outcome.endswith("_RECONCILIATION_REQUIRED") else "FAILED"
            statement = statement.values(status=status, failure_code=outcome)
        else:
            statement = statement.values(
                status="SUCCEEDED",
                external_id=outcome.external_id,
                provider_target_id=outcome.provider_target_id,
            )
        finished_at = func.greatest(LegacyPublicationAttempt.started_at, func.clock_timestamp())
        await self.session.execute(statement.values(finished_at=finished_at))
