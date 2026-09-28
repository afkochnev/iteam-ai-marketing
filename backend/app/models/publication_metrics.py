from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDTimestampMixin
from app.models.content import ContentChannel


class MetricsSource(StrEnum):
    PROVIDER = "PROVIDER"
    MANUAL = "MANUAL"


class PublicationMetricsSnapshot(UUIDTimestampMixin, Base):
    """Append-only observation of provider metrics for one immutable publication."""

    __tablename__ = "publication_metrics_snapshots"
    __table_args__ = (
        Index("ix_publication_metrics_publication_id", "publication_id"),
        Index("ix_publication_metrics_observed_at", "observed_at"),
        Index("ix_publication_metrics_publication_observed", "publication_id", "observed_at"),
    )

    publication_id: Mapped[UUID] = mapped_column(
        ForeignKey("publications.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[ContentChannel] = mapped_column(
        Enum(ContentChannel, name="content_channel", create_type=False), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    views: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)
    reactions: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)
    clicks: Mapped[int | None] = mapped_column(Integer)
    subscribers: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[MetricsSource] = mapped_column(
        Enum(MetricsSource, name="metrics_source"), nullable=False
    )
    provider: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
