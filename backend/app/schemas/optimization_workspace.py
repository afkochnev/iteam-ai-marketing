from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class OptimizationWorkspaceItem(BaseModel):
    campaign_id: UUID
    campaign_name: str
    category: str
    count: int
    href: str
    latest_at: datetime | None = None


class OptimizationDashboard(BaseModel):
    counts: dict[str, int] = Field(
        default_factory=lambda: dict.fromkeys(
            [
                "campaigns_with_new_results",
                "analyses_waiting_review",
                "actions_waiting_decision",
                "actions_waiting_apply",
                "experiments_running",
            ],
            0,
        )
    )
    items: list[OptimizationWorkspaceItem] = Field(default_factory=list)
