"""Read exact persisted links. Never infer provenance from names or mutable workflow state."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent_run import AgentRun
from app.models.campaign import Campaign
from app.models.content import ContentItem, ContentVersion
from app.models.marketing_experiment import MarketingExperiment, MarketingExperimentPublication
from app.models.marketing_feedback import MarketingFeedback, MarketingFeedbackAnalysis
from app.models.optimization import (
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
    OptimizationActionType,
)
from app.models.publication import Publication
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.models.publication_plan import PublicationPlan, PublicationPlanItem
from app.models.task import Task
from app.schemas.optimization import OptimizationActionResponse
from app.schemas.optimization_provenance import (
    OptimizationProvenanceResponse,
    ProvenanceAnalysis,
    ProvenanceNode,
    ProvenanceRecommendation,
)
from app.services.campaign_performance_service import RAW_FIELDS
from app.services.optimization_workspace_service import evidence_ids


class OptimizationProvenanceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(
        self, action_id: UUID, campaign_id: UUID | None = None
    ) -> OptimizationProvenanceResponse:
        action = await self.session.get(CampaignOptimizationAction, action_id)
        proposal = (
            await self.session.get(CampaignOptimizationProposal, action.proposal_id)
            if action
            else None
        )
        analysis = (
            await self.session.get(MarketingFeedbackAnalysis, proposal.feedback_analysis_id)
            if proposal
            else None
        )
        if (
            action is None
            or proposal is None
            or analysis is None
            or analysis.campaign_id != proposal.campaign_id
            or (campaign_id is not None and proposal.campaign_id != campaign_id)
        ):
            raise AppError("OPTIMIZATION_ACTION_NOT_FOUND", "Рекомендация не найдена.", 404)
        campaign = await self.session.get(Campaign, proposal.campaign_id)
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        cid = campaign.id
        base = f"/campaigns/{cid}"
        frozen = analysis.input_snapshot
        allowed = {
            "metrics_snapshot": evidence_ids(
                frozen, "publication_metrics_snapshot_ids", "metrics_snapshot_ids"
            ),
            "marketing_feedback": evidence_ids(frozen, "marketing_feedback_ids", "feedback_ids"),
            "publication": set(frozen.get("publication_ids") or []),
            "content_version": set(frozen.get("content_version_ids") or []),
        }
        limitations: list[str] = []
        evidence: list[ProvenanceNode] = []
        for ref in action.evidence_refs:
            kind, rid = ref.get("type"), ref.get("id")
            if kind not in allowed or str(rid) not in allowed[kind]:
                limitations.append("Ссылка evidence отсутствует в frozen analysis и не разрешена.")
                continue
            try:
                ident = UUID(str(rid))
            except ValueError:
                limitations.append("Некорректный идентификатор evidence.")
                continue
            node = await self._evidence(str(kind), ident, cid, frozen)
            if node is None:
                limitations.append(
                    f"Evidence {kind} {ident}: связь отсутствует или принадлежит другой кампании."
                )
            else:
                evidence.append(node)
        artifacts: list[ProvenanceNode] = []
        versions: dict[UUID, ContentVersion] = {}
        tasks = list(
            await self.session.scalars(
                select(Task)
                .where(Task.optimization_action_id == action.id, Task.campaign_id == cid)
                .order_by(Task.created_at, Task.id)
            )
        )
        runs = list(
            await self.session.scalars(
                select(AgentRun)
                .where(AgentRun.task_id.in_([t.id for t in tasks]), AgentRun.campaign_id == cid)
                .order_by(AgentRun.created_at, AgentRun.id)
            )
        )
        for task in tasks:
            artifacts.append(
                ProvenanceNode(
                    id=task.id,
                    type="TASK",
                    title=task.title,
                    status=task.status.value,
                    href=f"/tasks/{task.id}",
                )
            )
        for run in runs:
            artifacts.append(
                ProvenanceNode(
                    id=run.id,
                    type="AGENT_RUN",
                    title="Выполнение задачи",
                    status=run.status.value,
                    href=f"/tasks/{run.task_id}",
                    details={"task_id": str(run.task_id)},
                )
            )
        plans = list(
            await self.session.scalars(
                select(PublicationPlan)
                .where(
                    PublicationPlan.optimization_action_id == action.id,
                    PublicationPlan.campaign_id == cid,
                )
                .order_by(PublicationPlan.created_at, PublicationPlan.id)
            )
        )
        plan_items = list(
            await self.session.scalars(
                select(PublicationPlanItem)
                .where(PublicationPlanItem.publication_plan_id.in_([p.id for p in plans]))
                .order_by(PublicationPlanItem.position, PublicationPlanItem.id)
            )
        )
        for plan in plans:
            artifacts.append(
                ProvenanceNode(
                    id=plan.id,
                    type="PUBLICATION_PLAN",
                    title="Медиаплан",
                    status=plan.status.value,
                    href=f"{base}/plan#publication-plan-{plan.id}",
                    details={
                        "feedback_analysis_id": str(plan.feedback_analysis_id)
                        if plan.feedback_analysis_id
                        else None,
                        "optimization_proposal_id": str(plan.optimization_proposal_id)
                        if plan.optimization_proposal_id
                        else None,
                    },
                )
            )
        for item in plan_items:
            artifacts.append(
                ProvenanceNode(
                    id=item.id,
                    type="PLAN_ITEM",
                    title=item.topic,
                    status=item.status.value,
                    href=f"{base}/plan#plan-item-{item.id}",
                    details={"publication_plan_id": str(item.publication_plan_id)},
                )
            )
        content_conditions = []
        if tasks:
            content_conditions.append(ContentItem.source_task_id.in_([t.id for t in tasks]))
        if plan_items:
            content_conditions.append(
                ContentItem.metadata_["publication_plan_item_id"]
                .as_string()
                .in_([str(i.id) for i in plan_items])
            )
        from sqlalchemy import or_

        contents = (
            list(
                await self.session.scalars(
                    select(ContentItem).where(
                        ContentItem.campaign_id == cid, or_(*content_conditions)
                    )
                )
            )
            if content_conditions
            else []
        )
        # A revision can append a version to an existing item whose source_task_id is older.
        linked_versions = list(
            await self.session.scalars(
                select(ContentVersion)
                .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
                .where(
                    ContentItem.campaign_id == cid,
                    or_(
                        ContentVersion.content_item_id.in_([c.id for c in contents]),
                        ContentVersion.source_agent_run_id.in_([r.id for r in runs]),
                    ),
                )
                .order_by(ContentVersion.version_number, ContentVersion.id)
            )
        )
        versions.update({v.id: v for v in linked_versions})
        if action.target_version_id is not None:
            target = await self.session.scalar(
                select(ContentVersion)
                .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
                .where(
                    ContentVersion.id == action.target_version_id, ContentItem.campaign_id == cid
                )
            )
            if target is not None:
                versions[target.id] = target
        experiments = list(
            await self.session.scalars(
                select(MarketingExperiment).where(
                    MarketingExperiment.source_optimization_action_id == action.id,
                    MarketingExperiment.campaign_id == cid,
                )
            )
        )
        for experiment in experiments:
            artifacts.append(
                ProvenanceNode(
                    id=experiment.id,
                    type="MARKETING_EXPERIMENT",
                    title=experiment.hypothesis,
                    status=experiment.status.value,
                    href=f"{base}/performance#experiment-{experiment.id}",
                    details={
                        "result_data": experiment.result_data,
                        "limitations": experiment.limitations,
                        "semantics": "descriptive/non-causal",
                    },
                )
            )
        links = list(
            await self.session.scalars(
                select(MarketingExperimentPublication).where(
                    MarketingExperimentPublication.experiment_id.in_([e.id for e in experiments])
                )
            )
        )
        for link in links:
            version = await self.session.scalar(
                select(ContentVersion)
                .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
                .where(ContentVersion.id == link.content_version_id, ContentItem.campaign_id == cid)
            )
            if version:
                versions[version.id] = version
        publications = list(
            await self.session.scalars(
                select(Publication)
                .where(
                    Publication.campaign_id == cid,
                    Publication.content_version_id.in_(list(versions)),
                )
                .order_by(Publication.created_at, Publication.id)
            )
        )
        # Experiment relation names an exact publication, not any other publication of that version.
        if experiments and not tasks and not plans:
            publications = [p for p in publications if p.id in {x.publication_id for x in links}]
        titles_rows = (
            await self.session.execute(
                select(ContentItem.id, ContentItem.title).where(
                    ContentItem.campaign_id == cid,
                    ContentItem.id.in_([v.content_item_id for v in versions.values()]),
                )
            )
        ).all()
        titles = {key: title for key, title in titles_rows}
        version_nodes = [
            ProvenanceNode(
                id=v.id,
                type="CONTENT_VERSION",
                title=f"{titles.get(v.content_item_id, 'Материал')} · v{v.version_number}",
                href=f"/content/{v.content_item_id}#version-{v.id}",
                details={
                    "content_item_id": str(v.content_item_id),
                    "version_number": v.version_number,
                    "source_agent_run_id": str(v.source_agent_run_id)
                    if v.source_agent_run_id
                    else None,
                    "source_target": v.id == action.target_version_id,
                },
            )
            for v in versions.values()
        ]
        publication_nodes = [
            ProvenanceNode(
                id=p.id,
                type="PUBLICATION",
                title=f"{titles.get(p.content_item_id, 'Материал')} · {p.channel.value}",
                status=p.status.value,
                href=f"{base}/publications#publication-{p.id}",
                details={
                    "content_item_id": str(p.content_item_id),
                    "content_version_id": str(p.content_version_id),
                    "channel": p.channel.value,
                    "published_at": p.published_at.isoformat() if p.published_at else None,
                    "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None,
                },
            )
            for p in publications
        ]
        if action.type == OptimizationActionType.STRATEGY_REVIEW:
            limitations.append(
                "Task/AgentRun связаны с action. У Approval/strategy version нет прямого ключа "
                "к этой задаче; более поздние стратегии/планы не выводятся по времени или сходству."
            )
        if not artifacts and action.type != OptimizationActionType.NO_CHANGE:
            limitations.append("Downstream artifact ещё не создан.")
        index = action.source_recommendation_index
        rec: dict[str, Any] = (
            analysis.recommendations[index] if 0 <= index < len(analysis.recommendations) else {}
        )
        if not rec:
            limitations.append("Исходная рекомендация по сохранённому индексу отсутствует.")
        return OptimizationProvenanceResponse(
            campaign=ProvenanceNode(
                id=cid,
                type="CAMPAIGN",
                title=campaign.name,
                status=campaign.status.value,
                href=base,
            ),
            analysis=ProvenanceAnalysis(
                id=analysis.id,
                status=analysis.status.value,
                strategy_version=analysis.strategy_version,
                evidence_fingerprint=analysis.evidence_fingerprint,
                analysis_period_start=frozen.get("analysis_period_start"),
                analysis_period_end=frozen.get("analysis_period_end"),
                data_quality=frozen.get("data_quality"),
            ),
            source_recommendation=ProvenanceRecommendation(
                index=index,
                text=rec.get("recommendation"),
                category=rec.get("category"),
                expected_effect=rec.get("expected_effect"),
                priority=rec.get("priority"),
            ),
            proposal=ProvenanceNode(
                id=proposal.id,
                type="PROPOSAL",
                title=proposal.summary,
                status=proposal.status.value,
                href=f"{base}/performance#proposal-{proposal.id}",
            ),
            action=OptimizationActionResponse.model_validate(action),
            resolved_evidence=evidence,
            downstream_artifacts=artifacts,
            content_versions=version_nodes,
            publications=publication_nodes,
            limitations=limitations,
        )

    async def _evidence(
        self, kind: str, ident: UUID, cid: UUID, frozen: dict[str, Any]
    ) -> ProvenanceNode | None:
        if kind == "metrics_snapshot":
            row = await self.session.get(PublicationMetricsSnapshot, ident)
            pub = await self.session.get(Publication, row.publication_id) if row else None
            if row is None or pub is None or pub.campaign_id != cid:
                return None
            return ProvenanceNode(
                id=ident,
                type=kind,
                title=f"Метрики · {row.channel.value}",
                details={
                    "publication_id": str(pub.id),
                    "content_version_id": str(pub.content_version_id),
                    "channel": row.channel.value,
                    "observed_at": row.observed_at.isoformat(),
                    "source": row.source.value,
                    "provider": row.provider,
                    "metrics": {key: getattr(row, key) for key in RAW_FIELDS},
                },
            )
        if kind == "marketing_feedback":
            row_f = await self.session.get(MarketingFeedback, ident)
            if row_f is None or row_f.campaign_id != cid:
                return None
            frozen_f = next(
                (x for x in frozen.get("feedback", []) if x.get("id") == str(ident)), None
            )
            # Human feedback can change. Only display frozen category/rating, never private text.
            details = {
                "category": frozen_f.get("category") if frozen_f else None,
                "rating": frozen_f.get("rating") if frozen_f else None,
                "observed_at": frozen_f.get("observed_at") if frozen_f else None,
                "publication_id": str(row_f.publication_id) if row_f.publication_id else None,
                "content_item_id": str(row_f.content_item_id) if row_f.content_item_id else None,
                "content_version_id": str(row_f.content_version_id)
                if row_f.content_version_id
                else None,
            }
            return ProvenanceNode(id=ident, type=kind, title="Обратная связь", details=details)
        if kind == "publication":
            row_p = await self.session.get(Publication, ident)
            if row_p is None or row_p.campaign_id != cid:
                return None
            return ProvenanceNode(
                id=ident,
                type=kind,
                title=f"Публикация · {row_p.channel.value}",
                status=row_p.status.value,
                href=f"/campaigns/{cid}/publications#publication-{ident}",
                details={
                    "content_version_id": str(row_p.content_version_id),
                    "channel": row_p.channel.value,
                },
            )
        row_v = await self.session.scalar(
            select(ContentVersion)
            .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
            .where(ContentVersion.id == ident, ContentItem.campaign_id == cid)
        )
        if row_v is None:
            return None
        return ProvenanceNode(
            id=ident,
            type=kind,
            title=f"Версия v{row_v.version_number}",
            href=f"/content/{row_v.content_item_id}",
            details={
                "content_item_id": str(row_v.content_item_id),
                "version_number": row_v.version_number,
            },
        )
