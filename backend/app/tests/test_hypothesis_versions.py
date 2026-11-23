from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from app.models.campaign import Campaign
from app.models.hypothesis import HypothesisVersion, MarketingHypothesis
from app.models.user import User, UserRole
from app.models.workspace import Workspace

pytestmark = pytest.mark.integration


async def test_versions_preserve_the_exact_subject_and_reject_stale_writers(db_session):
    workspace = Workspace(
        id=UUID("4183ac32-0f61-54da-ad9e-ebb31696101b"), name="Test", slug="test", is_default=True
    )
    user = User(
        email="owner@example.org", full_name="Owner", password_hash="test", role=UserRole.MANAGER
    )
    db_session.add_all([workspace, user])
    await db_session.flush()
    campaign = Campaign(name="Trial", goal="Retention", created_by=user.id)
    db_session.add(campaign)
    await db_session.flush()
    root = MarketingHypothesis(workspace_id=workspace.id, campaign_id=campaign.id)
    db_session.add(root)
    await db_session.flush()
    snapshot = {
        "audience": "New customers",
        "problem": "Missing setup steps",
        "intervention": "Guided setup",
        "causal_mechanism": "Earlier activation",
        "kpi": "Day-seven retention",
        "expected_direction": "increase",
        "expected_effect": "5 percentage points",
        "baseline": "30%",
        "target": "35%",
        "measurement_window": "7 days",
        "verification_criterion": "Observed lift reaches target",
        "assumptions": [],
        "exclusions": ["Internal accounts"],
        "evidence_references": [],
    }
    defaults = {
        "hypothesis_id": root.id,
        "workspace_id": workspace.id,
        "campaign_id": campaign.id,
        "schema_version": 1,
        "snapshot": snapshot,
        "created_by": user.id,
        "source": "operator",
    }

    async def append(number, previous=None, **changes):
        version = HypothesisVersion(
            **(defaults | changes), version_number=number, supersedes_version_id=previous
        )
        db_session.add(version)
        await db_session.flush()
        return version

    first = await append(1)
    second = await append(2, first.id, snapshot=snapshot | {"target": "37%"})
    assert len(first.snapshot_hash) == 64
    assert first.snapshot_hash != second.snapshot_hash
    for number, previous, changes in [
        (2, first.id, {}),
        (3, first.id, {}),
        (3, second.id, {"campaign_id": uuid4()}),
        (3, second.id, {"snapshot": snapshot | {"target": ""}}),
        (3, second.id, {"snapshot": snapshot | {"assumptions": [False]}}),
    ]:
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await append(number, previous, **changes)
    for statement in [
        update(HypothesisVersion).where(HypothesisVersion.id == first.id).values(source="changed"),
        delete(HypothesisVersion).where(HypothesisVersion.id == second.id),
    ]:
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await db_session.execute(statement)
    await db_session.refresh(first)
    assert first.snapshot == snapshot
