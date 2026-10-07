"use client";

import { useEffect, useState } from "react";
import { optimizationApi, type FeedbackAnalysis, type OptimizationActionType, type OptimizationProposal } from "@/lib/api";
import { FriendlyError } from "@/components/friendly-error";

const types: Record<OptimizationActionType, string> = {CONTENT_REVISION: "Доработка контента", PUBLICATION_PLAN_REVISION: "Пересмотр медиаплана", STRATEGY_REVIEW: "Обзор стратегии", EXPERIMENT: "Предложение эксперимента", NO_CHANGE: "Без изменений"};
const targets = {CONTENT_ITEM: "Контент", PUBLICATION_PLAN: "Медиаплан", CAMPAIGN_STRATEGY: "Стратегия кампании", CAMPAIGN: "Кампания"};
const evidenceLabels = {publication: "Публикация", content_version: "Версия контента", metrics_snapshot: "Снимок метрик", marketing_feedback: "Обратная связь"};
const statuses = {PROPOSED: "Ожидает решения", APPROVED: "Принято", REJECTED: "Отклонено", APPLIED: "Применено", FAILED: "Ошибка"};

export function OptimizationProposalPanel({analysis, archived}: {analysis: FeedbackAnalysis; archived: boolean}) {
  const [proposal, setProposal] = useState<OptimizationProposal | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    void optimizationApi.list(analysis.campaign_id).then(rows => {if (active) setProposal(rows.find(row => row.feedback_analysis_id === analysis.id) ?? null);}).catch(reason => {if (active) setError(reason instanceof Error ? reason.message : "Не удалось загрузить рекомендации.");});
    return () => {active = false;};
  }, [analysis.campaign_id, analysis.id]);
  async function decide(id: string, decision: "approve" | "reject") {
    if (!proposal || busy) return;
    setBusy(true); setError("");
    try {await optimizationApi[decision](id); setProposal(await optimizationApi.get(proposal.id));}
    catch (reason) {setError(reason instanceof Error ? reason.message : "Не удалось сохранить решение.");}
    finally {setBusy(false);}
  }
  if (!proposal) return error ? <FriendlyError error={error} /> : null;
  return <section aria-label="Рекомендации к изменениям">
    <h3>Рекомендации к изменениям</h3>
    <p className="notice">Принятие рекомендации пока только фиксирует решение. Изменения в кампании будут создаваться отдельным подтверждённым workflow.</p>
    <p>Стратегия версии {proposal.strategy_version} · Статус предложения: {proposal.status}</p>
    {error && <FriendlyError error={error} />}
    <div className="optimization-actions">{proposal.actions.map(action => <article className="card optimization-action" key={action.id} aria-label={types[action.type]}>
      <h4>{types[action.type]}</h4>
      <p>{targets[action.target_entity_type]}: {action.target_entity_id}{action.target_version_id ? ` · версия ${action.target_version_id}` : ""}</p>
      <p><strong>Причина:</strong> {action.reason}</p><p><strong>Ожидаемый эффект:</strong> {action.expected_effect}</p><p><strong>Приоритет:</strong> {action.priority}</p>
      <p><strong>Доказательства из принятого анализа:</strong></p>
      {action.evidence_refs.length ? <ul>{action.evidence_refs.map(ref => <li key={`${ref.type}:${ref.id}`}>{evidenceLabels[ref.type]}: {ref.id}</li>)}</ul> : <p>Доказательств недостаточно для уверенного изменения.</p>}
      <p><strong>Статус:</strong> {statuses[action.status]}</p>
      <div className="actions"><button disabled={busy || archived || action.status !== "PROPOSED"} onClick={() => decide(action.id, "approve")}>Принять</button><button className="secondary" disabled={busy || archived || action.status !== "PROPOSED"} onClick={() => decide(action.id, "reject")}>Отклонить</button></div>
    </article>)}</div>
  </section>;
}
