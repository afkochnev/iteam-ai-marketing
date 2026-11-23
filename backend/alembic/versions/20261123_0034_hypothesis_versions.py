"""Add draft hypothesis identities and immutable, scoped snapshots."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261123_0034"
down_revision = "20261123_0033"
branch_labels = None
depends_on = None

TEXT_FIELDS = (
    "audience",
    "problem",
    "intervention",
    "causal_mechanism",
    "kpi",
    "expected_direction",
    "expected_effect",
    "baseline",
    "target",
    "measurement_window",
    "verification_criterion",
)
LIST_FIELDS = ("assumptions", "exclusions", "evidence_references")


def upgrade() -> None:
    op.create_table(
        "marketing_hypotheses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Legacy campaigns belong to the deterministic workspace seeded by revision 0030.
        sa.CheckConstraint(
            "workspace_id = '4183ac32-0f61-54da-ad9e-ebb31696101b'", name="legacy_workspace"
        ),
        sa.UniqueConstraint("id", "workspace_id", "campaign_id"),
    )
    op.create_index("ix_marketing_hypotheses_campaign_id", "marketing_hypotheses", ["campaign_id"])
    op.create_table(
        "hypothesis_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("hypothesis_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), sa.ForeignKey("hypothesis_versions.id")),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("snapshot_hash", sa.String(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id", "workspace_id", "campaign_id"],
            [
                "marketing_hypotheses.id",
                "marketing_hypotheses.workspace_id",
                "marketing_hypotheses.campaign_id",
            ],
        ),
        sa.UniqueConstraint("hypothesis_id", "version_number"),
        sa.CheckConstraint("schema_version = 1 AND version_number > 0", name="version_range"),
        sa.CheckConstraint("source ~ '\\S'", name="provenance"),
    )
    text_keys = ", ".join(f"'{key}'" for key in TEXT_FIELDS)
    list_keys = ", ".join(f"'{key}'" for key in LIST_FIELDS)
    op.execute(f"""
        CREATE FUNCTION validate_hypothesis_version() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE key text;
        BEGIN
            IF jsonb_typeof(NEW.snapshot) <> 'object' THEN
                RAISE EXCEPTION 'Expected hypothesis snapshot object' USING ERRCODE = '23514';
            END IF;
            FOREACH key IN ARRAY ARRAY[{text_keys}] LOOP
                IF jsonb_typeof(NEW.snapshot -> key) IS DISTINCT FROM 'string'
                    OR (NEW.snapshot ->> key) !~ '\\S' THEN
                    RAISE EXCEPTION 'Invalid snapshot field %', key USING ERRCODE = '23514';
                END IF;
            END LOOP;
            FOREACH key IN ARRAY ARRAY[{list_keys}] LOOP
                IF jsonb_typeof(NEW.snapshot -> key) IS DISTINCT FROM 'array' THEN
                    RAISE EXCEPTION 'Invalid snapshot list %', key USING ERRCODE = '23514';
                END IF;
                IF EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.snapshot -> key) AS x(value)
                           WHERE jsonb_typeof(value) <> 'string'
                              OR (value #>> '{{}}') !~ '\\S') THEN
                    RAISE EXCEPTION 'Invalid snapshot entry %', key USING ERRCODE = '23514';
                END IF;
            END LOOP;
            IF (NEW.version_number = 1 AND NEW.supersedes_version_id IS NOT NULL)
                OR (NEW.version_number > 1 AND NOT EXISTS (
                    SELECT 1 FROM hypothesis_versions WHERE id = NEW.supersedes_version_id
                    AND hypothesis_id = NEW.hypothesis_id AND version_number = NEW.version_number-1
                )) THEN
                RAISE EXCEPTION 'Invalid hypothesis predecessor' USING ERRCODE = '23514';
            END IF;
            NEW.snapshot_hash := encode(sha256(convert_to(
                jsonb_build_array(NEW.schema_version, NEW.snapshot)::text, 'UTF8')), 'hex');
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER validate_hypothesis_version BEFORE INSERT ON hypothesis_versions
        FOR EACH ROW EXECUTE FUNCTION validate_hypothesis_version()
    """)
    op.execute("""
        CREATE FUNCTION reject_hypothesis_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Hypothesis identity and versions are immutable' USING ERRCODE='23514';
        END $$;
    """)
    for table in ("marketing_hypotheses", "hypothesis_versions"):
        op.execute(f"""
            CREATE TRIGGER reject_hypothesis_mutation BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_hypothesis_mutation()
        """)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("LOCK TABLE marketing_hypotheses, hypothesis_versions IN ACCESS EXCLUSIVE MODE")
    )
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM marketing_hypotheses)")):
        raise RuntimeError("Refusing to drop recorded marketing hypotheses")
    op.drop_table("hypothesis_versions")
    op.drop_table("marketing_hypotheses")
    op.execute("DROP FUNCTION validate_hypothesis_version()")
    op.execute("DROP FUNCTION reject_hypothesis_mutation()")
