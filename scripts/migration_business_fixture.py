"""Inert, linked business rows for the disposable 0028 migration fixture."""

from __future__ import annotations

from typing import Any

from release_ops import require

REQUIRED_BUSINESS_TABLES = frozenset(
    {
        "campaigns",
        "tasks",
        "content_items",
        "content_versions",
        "approvals",
        "publications",
        "activity_logs",
    }
)

# This transaction only creates synthetic data. No scheduler, agent run, provider
# credential or publishable publication is needed to test preservation of these
# relationships and nonempty business payloads across restore/up/down.
BUSINESS_FIXTURE_SQL = """
BEGIN;
DO $fixture$
DECLARE
    fixture_user uuid := 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
    writer uuid;
    campaign uuid := '00000000-0000-4000-8000-0000000000a1';
    task uuid := '00000000-0000-4000-8000-0000000000a2';
    item uuid := '00000000-0000-4000-8000-0000000000a3';
    version uuid := '00000000-0000-4000-8000-0000000000a4';
    approval uuid := '00000000-0000-4000-8000-0000000000a5';
    publication uuid := '00000000-0000-4000-8000-0000000000a6';
    activity uuid := '00000000-0000-4000-8000-0000000000a7';
    fixture_time timestamptz := '2026-01-01T00:00:00Z';
BEGIN
    IF current_database() <> 'iteam_migration_drill'
       OR (SELECT count(*) FROM alembic_version) <> 1
       OR (SELECT version_num FROM alembic_version) <> '20261009_0028'
    THEN
        RAISE EXCEPTION 'NOT_A_SOURCE_MIGRATION_FIXTURE';
    END IF;
    SELECT id INTO STRICT writer FROM agents WHERE slug = 'writer';
    IF NOT EXISTS (
        SELECT 1 FROM users WHERE id = fixture_user
        AND role = 'MANAGER' AND is_active
    ) THEN
        RAISE EXCEPTION 'MIGRATION_FIXTURE_USER_MISSING';
    END IF;

    INSERT INTO campaigns
        (id, name, goal, status, strategy, strategy_version, created_by)
    VALUES
        (campaign, 'Migration rehearsal campaign', 'Preserve synthetic history',
         'ARCHIVED', '{"fixture":true,"goal":"preservation"}', 1, fixture_user);
    INSERT INTO tasks
        (id, campaign_id, task_type, title, assigned_agent_id, priority, status,
         input_data, output_data, completed_at)
    VALUES
        (task, campaign, 'MANUAL', 'Completed rehearsal task', writer, 'NORMAL',
         'COMPLETED', '{"fixture":true}', '{"result":"synthetic"}', fixture_time);
    INSERT INTO content_items
        (id, campaign_id, source_task_id, content_type, title, status,
         author_agent_id, channel, metadata, archived_at)
    VALUES
        (item, campaign, task, 'SOCIAL_POST', 'Archived rehearsal content',
         'ARCHIVED', writer, 'TELEGRAM', '{"fixture":true}', fixture_time);
    INSERT INTO content_versions
        (id, content_item_id, version_number, content, structured_content,
         created_by_agent_id, created_by_user_id)
    VALUES
        (version, item, 1, 'Synthetic rehearsal text; never publish.',
         '{"fixture":true,"text":"Preserve me"}', writer, fixture_user);
    UPDATE content_items SET current_version_id = version WHERE id = item;
    INSERT INTO approvals
        (id, object_type, object_id, subject_version, status,
         requested_by_agent_id, reviewed_by_user_id, subject_snapshot,
         metadata, resolved_at, comment)
    VALUES
        (approval, 'CONTENT_ITEM', item, 1, 'REJECTED', writer, fixture_user,
         '{"fixture":true,"title":"Archived rehearsal content"}',
         '{"fixture":true}', fixture_time, 'Synthetic rejection; do not publish');
    INSERT INTO publications
        (id, campaign_id, content_item_id, content_version_id, channel, status)
    VALUES
        (publication, campaign, item, version, 'TELEGRAM', 'CANCELLED');
    INSERT INTO activity_logs
        (id, event_type, campaign_id, task_id, agent_id, user_id,
         content_item_id, approval_id, operation_key, metadata)
    VALUES
        (activity, 'migration.rehearsal.fixture', campaign, task, writer,
         fixture_user, item, approval, 'migration-rehearsal-fixture',
         jsonb_build_object('fixture', true, 'publication_id', publication::text));
END;
$fixture$;
COMMIT;
"""


def seed_business_fixture(runtime: Any) -> None:
    """Populate only the source-schema database in the generated drill runtime."""
    runtime.compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-U",
        "iteam",
        "-d",
        "iteam_migration_drill",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        BUSINESS_FIXTURE_SQL,
    )


def require_business_fixture(counts: Any) -> None:
    """Prevent an empty-table preservation check from being accepted as proof."""
    require(
        type(counts) is dict
        and all(
            type(counts.get(table)) is int and counts[table] > 0
            for table in REQUIRED_BUSINESS_TABLES
        ),
        "BUSINESS_FIXTURE_NOT_REPRESENTATIVE",
    )
