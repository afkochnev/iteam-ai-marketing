"""Read-only attention classification; delivery history remains unchanged."""

from app.models.publication import Publication, PublicationStatus


def current_publication_failures(publications: list[Publication]) -> list[Publication]:
    """Suppress only a proven later delivery of the identical publication payload.

    PublicationService.create permits a new attempt after an ordinary failure.
    Its delivery identity is version/channel (and item for plan-bound posts).
    Requiring the same item and exact version also preserves the same persisted
    plan-item lineage, without guessing replacement relations across versions.
    Ambiguous deliveries always require explicit operator reconciliation.
    """
    published = [p for p in publications if p.status is PublicationStatus.PUBLISHED]
    return [
        failed
        for failed in publications
        if failed.status is PublicationStatus.FAILED
        and (
            failed.failure_code
            in {"TELEGRAM_RECONCILIATION_REQUIRED", "VK_RECONCILIATION_REQUIRED"}
            or not any(
                delivered.campaign_id == failed.campaign_id
                and delivered.content_item_id == failed.content_item_id
                and delivered.content_version_id == failed.content_version_id
                and delivered.channel == failed.channel
                and delivered.created_at > failed.created_at
                for delivered in published
            )
        )
    ]
