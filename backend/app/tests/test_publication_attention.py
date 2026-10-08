from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.content import ContentChannel, ContentItem, ContentVersion
from app.models.publication import Publication, PublicationStatus
from app.models.publication_plan import PublicationPlanItem
from app.services.campaign_workspace_service import CampaignWorkspaceService
from app.services.publication_attention_service import current_publication_failures
from app.tests.test_campaign_workspace import workspace_fixture
from app.tests.test_closed_loop_workspace import counts


@pytest.mark.parametrize(
    "case,suppressed",
    [
        ("no_successor", False),
        ("exact", True),
        ("telegram_reconciliation", False),
        ("vk_reconciliation", False),
        ("reconciled_not_published", False),
        ("other_plan_item", False),
        ("other_channel", False),
        ("other_version", False),
        ("earlier_success", False),
        ("non_plan_exact", True),
    ],
)
async def test_persisted_publication_attention(db_session, case, suppressed):
    data = await workspace_fixture(db_session)
    failed = data["publication"]
    failed.status = PublicationStatus.FAILED
    failed.failure_code = {
        "telegram_reconciliation": "TELEGRAM_RECONCILIATION_REQUIRED",
        "vk_reconciliation": "VK_RECONCILIATION_REQUIRED",
        "reconciled_not_published": "PUBLICATION_RECONCILED_NOT_PUBLISHED",
    }.get(case, "PROVIDER_ERROR")
    failed.retry_count = 2
    if case == "non_plan_exact":
        data["post"].metadata_ = {}
    if case not in {"no_successor", "reconciled_not_published"}:
        item_id, version_id = failed.content_item_id, failed.content_version_id
        if case == "other_plan_item":
            source = data["plan_item"]
            other_slot = PublicationPlanItem(
                **{
                    c.name: getattr(source, c.name)
                    for c in PublicationPlanItem.__table__.columns
                    if c.name not in {"id", "created_at", "updated_at", "position"}
                },
                position=2,
            )
            db_session.add(other_slot)
            await db_session.flush()
            post = data["post"]
            other_post = ContentItem(
                campaign_id=post.campaign_id,
                source_task_id=post.source_task_id,
                author_agent_id=post.author_agent_id,
                content_type=post.content_type,
                title=post.title,
                status=post.status,
                channel=post.channel,
                metadata_={"publication_plan_item_id": str(other_slot.id)},
            )
            db_session.add(other_post)
            await db_session.flush()
            other_version = ContentVersion(
                content_item_id=other_post.id,
                version_number=1,
                content="Other slot",
                structured_content={},
            )
            db_session.add(other_version)
            await db_session.flush()
            other_post.current_version_id = other_version.id
            item_id, version_id = other_post.id, other_version.id
        if case == "other_version":
            version = ContentVersion(
                content_item_id=item_id,
                version_number=2,
                content="Different payload",
                structured_content={},
            )
            db_session.add(version)
            await db_session.flush()
            version_id = version.id
        db_session.add(
            Publication(
                campaign_id=failed.campaign_id,
                content_item_id=item_id,
                content_version_id=version_id,
                channel=ContentChannel.TELEGRAM if case == "other_channel" else failed.channel,
                status=PublicationStatus.PUBLISHED,
                created_at=failed.created_at
                + timedelta(days=-1 if case == "earlier_success" else 1),
            )
        )
    await db_session.commit()
    before = await counts(db_session)
    publications = list((await db_session.scalars(select(Publication))).all())
    assert (failed not in current_publication_failures(publications)) is suppressed
    workspace = await CampaignWorkspaceService(db_session).get(data["campaign"].id)
    if suppressed:
        assert workspace.director.next_step.title != "Проверить отправку публикации"
    else:
        assert workspace.director.next_step.title == "Проверить отправку публикации"
        assert workspace.director.next_step.entity_id == failed.id
    assert await counts(db_session) == before
    assert await db_session.get(Publication, failed.id) is failed
    assert failed.status is PublicationStatus.FAILED and failed.retry_count == 2
    assert any(p.id == failed.id for p in workspace.publications)
