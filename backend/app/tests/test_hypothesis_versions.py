from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.core.database import async_session_factory
from app.models.campaign import Campaign
from app.models.hypothesis import HypothesisVersion, MarketingHypothesis
from app.models.user import User, UserRole
from app.models.workspace import Workspace

pytestmark = pytest.mark.integration


def hypothesis_snapshot():
    return {
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


def hypothesis_migration():
    directory = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))
    return directory.get_revision("20261123_0034").module


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
    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            hypothesis_migration().downgrade()

    connection = await db_session.connection()
    with pytest.raises(RuntimeError, match="Refusing to drop recorded marketing hypotheses"):
        await connection.run_sync(downgrade)
    assert await db_session.scalar(select(MarketingHypothesis.id)) == root.id

    snapshot = hypothesis_snapshot()
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
        (3, second.id, {"source": " \t\r\n "}),
        (3, second.id, {"snapshot": snapshot | {"target": ""}}),
        (3, second.id, {"snapshot": snapshot | {"verification_criterion": "\t\n"}}),
        (3, second.id, {"snapshot": snapshot | {"assumptions": ["\n"]}}),
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


async def test_two_transactions_compete_for_same_next_version(db_session):
    # Committed immutable rows must not escape into the shared test tables.
    schema = f"hypothesis_race_{uuid4().hex}"
    workspace_id = UUID("4183ac32-0f61-54da-ad9e-ebb31696101b")
    campaign_id, user_id, root_id, first_id = (uuid4() for _ in range(4))
    values = {
        "hypothesis_id": root_id,
        "workspace_id": workspace_id,
        "campaign_id": campaign_id,
        "source": "operator",
        "schema_version": 1,
        "snapshot": hypothesis_snapshot(),
        "created_by": user_id,
    }
    versions = HypothesisVersion.__table__
    path = text(f"SET LOCAL search_path TO {schema}")

    def install(connection):
        with Operations.context(MigrationContext.configure(connection)):
            hypothesis_migration().upgrade()

    async with db_session.begin():
        await db_session.execute(text(f"CREATE SCHEMA {schema}"))
        await db_session.execute(path)
        for table, identity in [
            ("workspaces", workspace_id),
            ("campaigns", campaign_id),
            ("users", user_id),
        ]:
            await db_session.execute(text(f"CREATE TABLE {table} (id uuid PRIMARY KEY)"))
            await db_session.execute(text(f"INSERT INTO {table} VALUES (:id)"), {"id": identity})
        await (await db_session.connection()).run_sync(install)
        await db_session.execute(
            MarketingHypothesis.__table__.insert().values(
                id=root_id, workspace_id=workspace_id, campaign_id=campaign_id
            )
        )
        await db_session.execute(versions.insert().values(**values, id=first_id, version_number=1))

    try:
        next_version = versions.insert().values(
            **values, supersedes_version_id=first_id, version_number=2
        )
        winner_id = uuid4()
        async with async_session_factory() as other:
            async with db_session.begin():
                await db_session.execute(path)
                await db_session.execute(next_version.values(id=winner_id))
                with pytest.raises(DBAPIError) as blocked:
                    async with other.begin():
                        await other.execute(path)
                        await other.execute(text("SET LOCAL lock_timeout = '500ms'"))
                        await other.execute(next_version.values(id=uuid4()))
                assert blocked.value.orig.sqlstate == "55P03"
            with pytest.raises(IntegrityError) as duplicate:
                async with other.begin():
                    await other.execute(path)
                    await other.execute(next_version.values(id=uuid4()))
            assert duplicate.value.orig.sqlstate == "23505"
            async with other.begin():
                await other.execute(path)
                rows = await other.scalars(
                    select(versions.c.id).order_by(versions.c.version_number)
                )
                assert list(rows) == [first_id, winner_id]
    finally:
        async with db_session.begin():
            await db_session.execute(text(f"DROP SCHEMA {schema} CASCADE"))
