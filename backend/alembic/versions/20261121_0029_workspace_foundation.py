"""Create the additive workspace access foundation."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261121_0029"
down_revision: str = "20261009_0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    workspace_role = postgresql.ENUM(
        "WORKSPACE_ADMIN",
        "MARKETING_MANAGER",
        name="workspace_role",
        create_type=False,
    )
    workspace_role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "workspaces",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    op.create_index("ix_workspaces_slug", "workspaces", [sa.text("lower(slug)")], unique=True)
    op.create_index(
        "uq_workspaces_one_default",
        "workspaces",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "workspace_memberships",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", workspace_role, nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_workspace_memberships_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_memberships_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_memberships")),
        sa.UniqueConstraint(
            "workspace_id", "user_id", name="uq_workspace_memberships_workspace_user"
        ),
    )
    op.create_index(
        "ix_workspace_memberships_workspace_id",
        "workspace_memberships",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_memberships_user_id",
        "workspace_memberships",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_memberships_role",
        "workspace_memberships",
        ["role"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_memberships_role", table_name="workspace_memberships")
    op.drop_index("ix_workspace_memberships_user_id", table_name="workspace_memberships")
    op.drop_index("ix_workspace_memberships_workspace_id", table_name="workspace_memberships")
    op.drop_table("workspace_memberships")
    op.drop_index("uq_workspaces_one_default", table_name="workspaces")
    op.drop_index("ix_workspaces_slug", table_name="workspaces")
    op.drop_table("workspaces")
    postgresql.ENUM(name="workspace_role").drop(op.get_bind(), checkfirst=True)
