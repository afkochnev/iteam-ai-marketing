from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MarketingHypothesis(Base):
    __tablename__ = "marketing_hypotheses"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", "campaign_id"),
        CheckConstraint(
            "workspace_id = '4183ac32-0f61-54da-ad9e-ebb31696101b'", name="legacy_workspace"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HypothesisVersion(Base):
    __tablename__ = "hypothesis_versions"
    __table_args__ = (
        UniqueConstraint("hypothesis_id", "version_number"),
        CheckConstraint("schema_version = 1 AND version_number > 0", name="version_range"),
        CheckConstraint("length(trim(source)) > 0", name="provenance"),
        ForeignKeyConstraint(
            ["hypothesis_id", "workspace_id", "campaign_id"],
            [
                "marketing_hypotheses.id",
                "marketing_hypotheses.workspace_id",
                "marketing_hypotheses.campaign_id",
            ],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    hypothesis_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    campaign_id: Mapped[UUID]
    version_number: Mapped[int]
    supersedes_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("hypothesis_versions.id"))
    schema_version: Mapped[int]
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    snapshot_hash: Mapped[str] = mapped_column(server_default=FetchedValue())
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
