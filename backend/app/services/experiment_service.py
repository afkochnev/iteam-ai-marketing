from datetime import UTC, datetime
from statistics import fmean
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.campaign import Campaign, CampaignStatus
from app.models.marketing_experiment import (
    ExperimentPublicationRole,
    ExperimentStatus,
    MarketingExperiment,
    MarketingExperimentPublication,
)
from app.models.optimization import (
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
    OptimizationActionStatus,
)
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.models.user import User
from app.schemas.experiment import (
    ExperimentConfiguration,
    ExperimentCreate,
    ExperimentPublicationResponse,
    ExperimentResponse,
)
from app.schemas.feedback import OptimizationExperimentSpec
from app.schemas.optimization import OptimizationActionApplyRequest, OptimizationActionApplyResponse
from app.services.activity_log_service import ActivityLogService

LEGACY_SPEC_MESSAGE = (
    "Для этой старой рекомендации отсутствуют структурированные параметры эксперимента. "
    "Сформируйте новый анализ на актуальных данных."
)


class ExperimentService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_from_action(
        self, campaign_id: UUID, user: User, data: ExperimentCreate
    ) -> ExperimentResponse:
        from app.services.optimization_apply_service import OptimizationApplyService

        action = await self.session.get(
            CampaignOptimizationAction, data.source_optimization_action_id
        )
        proposal = (
            await self.session.get(CampaignOptimizationProposal, action.proposal_id)
            if action
            else None
        )
        if proposal is None or proposal.campaign_id != campaign_id:
            raise AppError(
                "EXPERIMENT_ACTION_INVALID", "Рекомендация не принадлежит кампании.", 422
            )
        if action is None or action.type.value != "EXPERIMENT":
            raise AppError("EXPERIMENT_ACTION_INVALID", "Требуется рекомендация EXPERIMENT.", 422)
        result = await OptimizationApplyService(self.session).apply(
            action.id,
            user,
            OptimizationActionApplyRequest(
                experiment=ExperimentConfiguration.model_validate(
                    data.model_dump(exclude={"source_optimization_action_id"})
                )
            ),
        )
        assert result.experiment_id is not None
        return await self.response(await self.get(result.experiment_id))

    async def apply_locked(
        self,
        campaign: Campaign,
        proposal: CampaignOptimizationProposal,
        action: CampaignOptimizationAction,
        user: User,
        config: ExperimentConfiguration,
    ) -> OptimizationActionApplyResponse:
        """Caller owns Campaign → Proposal → Action locks and validated frozen evidence."""
        from app.services.optimization_apply_service import OptimizationApplyService
        from app.services.optimization_proposal_service import OptimizationProposalService

        if action.experiment_spec is None:
            raise AppError("OPTIMIZATION_EXPERIMENT_SPEC_MISSING", LEGACY_SPEC_MESSAGE, 409)
        spec = OptimizationExperimentSpec.model_validate(action.experiment_spec)
        publications = await self.validate_publications(campaign.id, config)
        experiment = MarketingExperiment(
            campaign_id=campaign.id,
            source_optimization_action_id=action.id,
            **spec.model_dump(),
            **config.model_dump(exclude={"baseline_publication_ids", "experiment_publication_ids"}),
            status=ExperimentStatus.DRAFT,
            created_by_user_id=user.id,
            limitations=[],
            result_data={},
        )
        self.session.add(experiment)
        await self.session.flush()
        for role, ids in [
            (ExperimentPublicationRole.BASELINE, config.baseline_publication_ids),
            (ExperimentPublicationRole.EXPERIMENT, config.experiment_publication_ids),
        ]:
            for pid in ids:
                self.session.add(
                    MarketingExperimentPublication(
                        experiment_id=experiment.id,
                        publication_id=pid,
                        role=role,
                        content_version_id=publications[pid].content_version_id,
                    )
                )
        await self.session.flush()
        # The artifact and all memberships are durable in the same commit as APPLIED.
        action.status = OptimizationActionStatus.APPLIED
        action.applied_by_user_id = user.id
        action.applied_at = datetime.now(UTC)
        await self.session.flush()
        await OptimizationProposalService(self.session).update_aggregation(proposal)
        await self.record("MARKETING_EXPERIMENT_CREATED", experiment, user_id=user.id)
        await ActivityLogService(self.session).record(
            "OPTIMIZATION_ACTION_APPLIED",
            operation_key=f"optimization-action-applied:{action.id}",
            campaign_id=campaign.id,
            user_id=user.id,
            metadata={
                "proposal_id": str(proposal.id),
                "action_id": str(action.id),
                "feedback_analysis_id": str(proposal.feedback_analysis_id),
                "action_type": action.type.value,
                "artifact_type": "MARKETING_EXPERIMENT",
                "artifact_id": str(experiment.id),
                "experiment_id": str(experiment.id),
            },
        )
        await self.session.commit()
        return await OptimizationApplyService(self.session).response(action)

    async def validate_publications(
        self, campaign_id: UUID, config: ExperimentConfiguration
    ) -> dict[UUID, Publication]:
        ids = config.baseline_publication_ids + config.experiment_publication_ids
        rows = list(
            await self.session.scalars(
                select(Publication)
                .where(Publication.id.in_(ids))
                .order_by(Publication.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(rows) != len(ids) or any(p.campaign_id != campaign_id for p in rows):
            raise AppError(
                "EXPERIMENT_PUBLICATIONS_INVALID", "Публикации должны принадлежать кампании.", 422
            )
        return {row.id: row for row in rows}

    async def get(self, experiment_id: UUID) -> MarketingExperiment:
        row = await self.session.get(MarketingExperiment, experiment_id)
        if row is None:
            raise AppError("EXPERIMENT_NOT_FOUND", "Эксперимент не найден.", 404)
        return row

    async def links(self, row: MarketingExperiment) -> list[MarketingExperimentPublication]:
        return list(
            await self.session.scalars(
                select(MarketingExperimentPublication)
                .where(MarketingExperimentPublication.experiment_id == row.id)
                .order_by(
                    MarketingExperimentPublication.role,
                    MarketingExperimentPublication.publication_id,
                )
            )
        )

    async def response(self, row: MarketingExperiment) -> ExperimentResponse:
        await self.session.refresh(row)
        action = await self.session.get(
            CampaignOptimizationAction, row.source_optimization_action_id
        )
        assert action is not None
        proposal = await self.session.get(CampaignOptimizationProposal, action.proposal_id)
        assert proposal is not None
        values = {
            key: getattr(row, key) for key in ExperimentResponse.model_fields if hasattr(row, key)
        }
        return ExperimentResponse(
            **values,
            proposal_id=proposal.id,
            feedback_analysis_id=proposal.feedback_analysis_id,
            publications=[
                ExperimentPublicationResponse.model_validate(link) for link in await self.links(row)
            ],
        )

    async def list_for_campaign(self, campaign_id: UUID) -> list[ExperimentResponse]:
        rows = await self.session.scalars(
            select(MarketingExperiment)
            .where(MarketingExperiment.campaign_id == campaign_id)
            .order_by(MarketingExperiment.created_at.desc(), MarketingExperiment.id)
        )
        return [await self.response(row) for row in rows]

    async def locked(self, experiment_id: UUID) -> MarketingExperiment:
        source = await self.get(experiment_id)
        campaign = await self.session.get(
            Campaign, source.campaign_id, with_for_update=True, populate_existing=True
        )
        if campaign is None or campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
        row = await self.session.get(
            MarketingExperiment, experiment_id, with_for_update=True, populate_existing=True
        )
        assert row is not None
        return row

    async def approve(self, experiment_id: UUID, user: User) -> ExperimentResponse:
        row = await self.locked(experiment_id)
        if row.status is ExperimentStatus.CANCELLED:
            raise AppError("EXPERIMENT_CANCELLED", "Отменённый эксперимент нельзя запустить.", 409)
        if row.status in {
            ExperimentStatus.APPROVED,
            ExperimentStatus.RUNNING,
            ExperimentStatus.COMPLETED,
        }:
            await self.session.commit()
            return await self.response(row)
        action = await self.session.get(
            CampaignOptimizationAction, row.source_optimization_action_id
        )
        if action is None or action.status is not OptimizationActionStatus.APPLIED:
            raise AppError("EXPERIMENT_ACTION_INVALID", "Исходная рекомендация не применена.", 409)
        links = await self.links(row)
        config = ExperimentConfiguration(
            baseline_start=row.baseline_start,
            baseline_end=row.baseline_end,
            experiment_start=row.experiment_start,
            experiment_end=row.experiment_end,
            baseline_publication_ids=[
                link.publication_id
                for link in links
                if link.role is ExperimentPublicationRole.BASELINE
            ],
            experiment_publication_ids=[
                link.publication_id
                for link in links
                if link.role is ExperimentPublicationRole.EXPERIMENT
            ],
        )
        publications = await self.validate_publications(row.campaign_id, config)
        if any(
            publications[link.publication_id].content_version_id != link.content_version_id
            for link in links
        ):
            raise AppError("EXPERIMENT_SOURCE_CHANGED", "Точная версия публикации изменилась.", 409)
        row.status = ExperimentStatus.APPROVED
        row.approved_by_user_id = user.id
        row.approved_at = datetime.now(UTC)
        await self.record("MARKETING_EXPERIMENT_APPROVED", row, user_id=user.id)
        await self.session.commit()
        return await self.response(row)

    async def cancel(self, experiment_id: UUID, user: User) -> ExperimentResponse:
        row = await self.locked(experiment_id)
        if row.status is ExperimentStatus.COMPLETED:
            raise AppError(
                "EXPERIMENT_ALREADY_COMPLETED", "Завершённый эксперимент нельзя отменить.", 409
            )
        if row.status is not ExperimentStatus.CANCELLED:
            row.status = ExperimentStatus.CANCELLED
            row.cancelled_by_user_id = user.id
            row.cancelled_at = datetime.now(UTC)
            await self.record("MARKETING_EXPERIMENT_CANCELLED", row, user_id=user.id)
        await self.session.commit()
        return await self.response(row)

    async def record(
        self, event: str, row: MarketingExperiment, *, user_id: UUID | None = None
    ) -> None:
        action = await self.session.get(
            CampaignOptimizationAction, row.source_optimization_action_id
        )
        assert action is not None
        proposal = await self.session.get(CampaignOptimizationProposal, action.proposal_id)
        assert proposal is not None
        links = await self.links(row)
        await ActivityLogService(self.session).record(
            event,
            operation_key=f"{event.lower()}:{row.id}",
            campaign_id=row.campaign_id,
            user_id=user_id,
            metadata={
                "experiment_id": str(row.id),
                "action_id": str(action.id),
                "proposal_id": str(proposal.id),
                "feedback_analysis_id": str(proposal.feedback_analysis_id),
                "success_metric": row.success_metric.value,
                "status": row.status.value,
                "publication_ids": [str(link.publication_id) for link in links],
                "publication_count": len(links),
                "used_snapshot_ids": row.result_data.get("used_snapshot_ids", []),
            },
        )

    async def advance(self, *, now: datetime | None = None, limit: int = 50) -> list[UUID]:
        now = now or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Lifecycle clock must be timezone-aware")
        ids = list(
            await self.session.scalars(
                select(MarketingExperiment.id)
                .where(
                    MarketingExperiment.status.in_(
                        [ExperimentStatus.APPROVED, ExperimentStatus.RUNNING]
                    ),
                    MarketingExperiment.experiment_start <= now,
                )
                .order_by(MarketingExperiment.experiment_start, MarketingExperiment.id)
                .limit(limit)
            )
        )
        changed = []
        for eid in ids:
            source = await self.get(eid)
            # Same Campaign → Experiment order as approval/cancel. One commit per
            # experiment so an enqueue/recovery-style batch cannot release others.
            campaign = await self.session.scalar(
                select(Campaign)
                .where(Campaign.id == source.campaign_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if campaign is None or campaign.status is CampaignStatus.ARCHIVED:
                await self.session.rollback()
                continue
            row = await self.session.scalar(
                select(MarketingExperiment)
                .where(MarketingExperiment.id == eid)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if row is None or row.status not in {
                ExperimentStatus.APPROVED,
                ExperimentStatus.RUNNING,
            }:
                await self.session.commit()
                continue
            if row.status is ExperimentStatus.APPROVED:
                row.status = ExperimentStatus.RUNNING
                await self.record("MARKETING_EXPERIMENT_STARTED", row)
                changed.append(eid)
            if now >= row.experiment_end:
                await self.evaluate(row)
                row.status = ExperimentStatus.COMPLETED
                await self.record("MARKETING_EXPERIMENT_COMPLETED", row)
                if eid not in changed:
                    changed.append(eid)
            await self.session.commit()
        return changed

    async def evaluate(self, row: MarketingExperiment) -> None:
        limitations = [
            "Только описательное сравнение.",
            "Статистическая значимость и причинный эффект не оцениваются.",
        ]
        groups: dict[str, Any] = {}
        used: list[str] = []
        links = await self.links(row)
        for role, start, end in [
            (ExperimentPublicationRole.BASELINE, row.baseline_start, row.baseline_end),
            (ExperimentPublicationRole.EXPERIMENT, row.experiment_start, row.experiment_end),
        ]:
            members = [link for link in links if link.role is role]
            values: list[int] = []
            observations: list[dict[str, Any]] = []
            observed = 0
            for link in members:
                publication = await self.session.get(Publication, link.publication_id)
                assert publication is not None
                if publication.published_at is None or not start <= publication.published_at <= end:
                    limitations.append(
                        f"{role.value}: публикация {publication.id} "
                        "вне ожидаемого окна или без published_at."
                    )
                snapshot = await self.session.scalar(
                    select(PublicationMetricsSnapshot)
                    .where(
                        PublicationMetricsSnapshot.publication_id == publication.id,
                        PublicationMetricsSnapshot.observed_at <= end,
                    )
                    .order_by(
                        PublicationMetricsSnapshot.observed_at.desc(),
                        PublicationMetricsSnapshot.created_at.desc(),
                        PublicationMetricsSnapshot.id.desc(),
                    )
                    .limit(1)
                )
                value = None
                snapshot_id = None
                if publication.content_version_id != link.content_version_id:
                    limitations.append(
                        f"{role.value}: точная версия публикации {publication.id} "
                        "изменилась; наблюдение исключено."
                    )
                elif publication.status is PublicationStatus.PUBLISHED and snapshot is not None:
                    observed += 1
                    snapshot_id = str(snapshot.id)
                    used.append(snapshot_id)
                    value = getattr(snapshot, row.success_metric.value.lower())
                    if value is not None:
                        values.append(value)
                observations.append(
                    {
                        "publication_id": str(publication.id),
                        "content_version_id": str(link.content_version_id),
                        "snapshot_id": snapshot_id,
                        "value": value,
                    }
                )
            n = len(members)
            if len(values) < n:
                limitations.append(
                    f"{role.value}: отсутствуют метрики; неполное покрытие ({len(values)}/{n})."
                )
            if len(values) < 3:
                limitations.append(
                    f"{role.value}: малое число наблюдений ({len(values)}); "
                    "осторожная интерпретация."
                )
            groups[role.value] = {
                "linked_publication_count": n,
                "observed_publication_count": observed,
                "metric_coverage": len(values) / n if n else 0,
                "sample_count": len(values),
                "mean": fmean(values) if values else None,
                "observations": observations,
            }
        baseline, experiment = groups["BASELINE"], groups["EXPERIMENT"]
        if baseline["linked_publication_count"] != experiment["linked_publication_count"]:
            limitations.append("Неравные размеры групп публикаций.")
        b, e = baseline["mean"], experiment["mean"]
        delta = e - b if b is not None and e is not None else None
        percent = delta / b * 100 if delta is not None and b > 0 else None
        if b == 0:
            limitations.append("Нулевое среднее baseline: процентная разница не рассчитывается.")
        if row.minimum_observation_requirement:
            limitations.append(
                "Текстовое минимальное требование к наблюдениям не проверяется автоматически."
            )
        if delta is None:
            summary = (
                "После завершения периода недостаточно наблюдений "
                f"для сравнения {row.success_metric.value}."
            )
        else:
            relative = f" ({percent:+.1f}%)" if percent is not None else ""
            summary = (
                f"Для метрики {row.success_metric.value} среднее наблюдаемое значение в baseline: "
                f"{b:.1f} по {baseline['sample_count']} публикациям; в experiment: "
                f"{e:.1f} по {experiment['sample_count']} публикациям. "
                f"Наблюдаемая разница: {delta:+.1f}{relative}. "
                "Это описательное сравнение, не доказательство причинного эффекта."
            )
        row.result_summary = summary
        row.limitations = list(dict.fromkeys(limitations))
        row.result_data = {
            "groups": groups,
            "absolute_delta": delta,
            "percent_delta": percent,
            "used_snapshot_ids": used,
        }
