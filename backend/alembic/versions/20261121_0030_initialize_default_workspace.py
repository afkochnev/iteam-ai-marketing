"""Initialize the deterministic single-workspace baseline."""

from collections.abc import Sequence
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping

from alembic import op

revision: str = "20261121_0030"
down_revision: str = "20261121_0029"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

DEFAULT_WORKSPACE_ID = UUID("4183ac32-0f61-54da-ad9e-ebb31696101b")
DEFAULT_WORKSPACE_NAME = "iTeam"
DEFAULT_WORKSPACE_SLUG = "iteam"

USER_ROLE = postgresql.ENUM(
    "ADMIN",
    "MANAGER",
    name="user_role",
    create_type=False,
)
WORKSPACE_ROLE = postgresql.ENUM(
    "WORKSPACE_ADMIN",
    "MARKETING_MANAGER",
    name="workspace_role",
    create_type=False,
)

USERS = sa.table(
    "users",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("role", USER_ROLE),
    sa.column("is_active", sa.Boolean()),
)
WORKSPACES = sa.table(
    "workspaces",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("name", sa.String(length=255)),
    sa.column("slug", sa.String(length=100)),
    sa.column("is_default", sa.Boolean()),
    sa.column("is_active", sa.Boolean()),
)
WORKSPACE_MEMBERSHIPS = sa.table(
    "workspace_memberships",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("workspace_id", postgresql.UUID(as_uuid=True)),
    sa.column("user_id", postgresql.UUID(as_uuid=True)),
    sa.column("role", WORKSPACE_ROLE),
    sa.column("is_active", sa.Boolean()),
)

ROLE_MAP = {
    "ADMIN": "WORKSPACE_ADMIN",
    "MANAGER": "MARKETING_MANAGER",
}


def _membership_id(user_id: UUID) -> UUID:
    return uuid5(DEFAULT_WORKSPACE_ID, f"membership:{user_id}")


def _is_exact_workspace(row: RowMapping) -> bool:
    return bool(
        row["id"] == DEFAULT_WORKSPACE_ID
        and row["name"] == DEFAULT_WORKSPACE_NAME
        and row["slug"].lower() == DEFAULT_WORKSPACE_SLUG
        and row["is_default"] is True
        and row["is_active"] is True
    )


def _initialize_default_workspace(bind: Connection) -> None:
    workspace_rows = list(
        bind.execute(
            sa.select(
                WORKSPACES.c.id,
                WORKSPACES.c.name,
                WORKSPACES.c.slug,
                WORKSPACES.c.is_default,
                WORKSPACES.c.is_active,
            )
        ).mappings()
    )
    if not workspace_rows:
        bind.execute(
            sa.insert(WORKSPACES).values(
                id=DEFAULT_WORKSPACE_ID,
                name=DEFAULT_WORKSPACE_NAME,
                slug=DEFAULT_WORKSPACE_SLUG,
                is_default=True,
                is_active=True,
            )
        )
    elif len(workspace_rows) != 1 or not _is_exact_workspace(workspace_rows[0]):
        raise RuntimeError("Conflicting workspace state blocks default workspace initialization")

    active_users = list(
        bind.execute(
            sa.select(USERS.c.id, USERS.c.role)
            .where(USERS.c.is_active.is_(True))
            .order_by(USERS.c.id)
        ).mappings()
    )
    expected_by_user: dict[UUID, tuple[UUID, str]] = {}
    for user in active_users:
        user_id = user["id"]
        role = ROLE_MAP.get(user["role"])
        if role is None:
            unsupported_role = user["role"]
            raise RuntimeError(
                f"Unsupported active user role for workspace backfill: {unsupported_role}"
            )
        membership_id = _membership_id(user_id)
        expected_by_user[user_id] = (membership_id, role)
        rows = list(
            bind.execute(
                sa.select(
                    WORKSPACE_MEMBERSHIPS.c.id,
                    WORKSPACE_MEMBERSHIPS.c.workspace_id,
                    WORKSPACE_MEMBERSHIPS.c.user_id,
                    WORKSPACE_MEMBERSHIPS.c.role,
                    WORKSPACE_MEMBERSHIPS.c.is_active,
                ).where(
                    sa.or_(
                        WORKSPACE_MEMBERSHIPS.c.id == membership_id,
                        sa.and_(
                            WORKSPACE_MEMBERSHIPS.c.workspace_id == DEFAULT_WORKSPACE_ID,
                            WORKSPACE_MEMBERSHIPS.c.user_id == user_id,
                        ),
                    )
                )
            ).mappings()
        )
        if not rows:
            bind.execute(
                sa.insert(WORKSPACE_MEMBERSHIPS).values(
                    id=membership_id,
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    user_id=user_id,
                    role=role,
                    is_active=True,
                )
            )
            continue
        if len(rows) != 1:
            raise RuntimeError("Conflicting workspace membership state blocks initialization")
        existing = rows[0]
        if not (
            existing["id"] == membership_id
            and existing["workspace_id"] == DEFAULT_WORKSPACE_ID
            and existing["user_id"] == user_id
            and existing["role"] == role
            and existing["is_active"] is True
        ):
            raise RuntimeError("Conflicting workspace membership state blocks initialization")

    stored_memberships = list(
        bind.execute(
            sa.select(
                WORKSPACE_MEMBERSHIPS.c.id,
                WORKSPACE_MEMBERSHIPS.c.user_id,
                WORKSPACE_MEMBERSHIPS.c.role,
                WORKSPACE_MEMBERSHIPS.c.is_active,
            ).where(WORKSPACE_MEMBERSHIPS.c.workspace_id == DEFAULT_WORKSPACE_ID)
        ).mappings()
    )
    stored_by_user = {row["user_id"]: row for row in stored_memberships}
    if set(stored_by_user) != set(expected_by_user):
        raise RuntimeError("Workspace membership count does not match active user count")
    for user_id, (membership_id, role) in expected_by_user.items():
        stored = stored_by_user[user_id]
        if not (
            stored["id"] == membership_id and stored["role"] == role and stored["is_active"] is True
        ):
            raise RuntimeError("Workspace membership validation failed")


def _remove_default_workspace(bind: Connection) -> None:
    workspace = (
        bind.execute(
            sa.select(
                WORKSPACES.c.id,
                WORKSPACES.c.name,
                WORKSPACES.c.slug,
                WORKSPACES.c.is_default,
                WORKSPACES.c.is_active,
            ).where(WORKSPACES.c.id == DEFAULT_WORKSPACE_ID)
        )
        .mappings()
        .one_or_none()
    )
    if workspace is None:
        return
    if not _is_exact_workspace(workspace):
        raise RuntimeError("Refusing to remove a non-canonical default workspace")
    bind.execute(
        sa.delete(WORKSPACE_MEMBERSHIPS).where(
            WORKSPACE_MEMBERSHIPS.c.workspace_id == DEFAULT_WORKSPACE_ID
        )
    )
    bind.execute(sa.delete(WORKSPACES).where(WORKSPACES.c.id == DEFAULT_WORKSPACE_ID))


def upgrade() -> None:
    _initialize_default_workspace(op.get_bind())


def downgrade() -> None:
    _remove_default_workspace(op.get_bind())
