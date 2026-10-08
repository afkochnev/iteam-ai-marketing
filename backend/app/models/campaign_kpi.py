from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDTimestampMixin
from app.models.content import ContentChannel


class KPIMetric(StrEnum):
    VIEWS = "VIEWS"
    IMPRESSIONS = "IMPRESSIONS"
    REACTIONS = "REACTIONS"
    LIKES = "LIKES"
    COMMENTS = "COMMENTS"
    SHARES = "SHARES"
    CLICKS = "CLICKS"
    SUBSCRIBERS = "SUBSCRIBERS"
    ENGAGEMENT_RATE = "ENGAGEMENT_RATE"
    CTR = "CTR"


class KPIComparison(StrEnum):
    GTE = "GTE"
    LTE = "LTE"


class CampaignKPI(UUIDTimestampMixin, Base):
    __tablename__ = "campaign_kpis"
    __table_args__ = (
        CheckConstraint("target_value >= 0", name="nonnegative_target"),
        CheckConstraint("period_start < period_end", name="ordered_period"),
        Index("ix_campaign_kpis_campaign_id", "campaign_id"),
    )
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    metric: Mapped[KPIMetric] = mapped_column(Enum(KPIMetric, name="kpi_metric"), nullable=False)
    channel: Mapped[ContentChannel | None] = mapped_column(
        Enum(ContentChannel, name="content_channel", create_type=False)
    )
    target_value: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    comparison: Mapped[KPIComparison] = mapped_column(
        Enum(KPIComparison, name="kpi_comparison"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
