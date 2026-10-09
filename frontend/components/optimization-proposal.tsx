"use client";

import { useEffect, useRef, useState } from "react";
import { optimizationApi, type FeedbackAnalysis, type OptimizationActionType, type OptimizationProposal } from "@/lib/api";
import { ExperimentCreateForm } from "@/components/marketing-experiments";
import { FriendlyError } from "@/components/friendly-error";

import { OptimizationProvenanceView } from "@/components/optimization-provenance";

const types: Record<OptimizationActionType, string> = {CONTENT_REVISION: "Доработка контента", PUBLICATION_PLAN_REVISION: "Пересмотр медиаплана", STRATEGY_REVIEW: "Обзор стратегии", EXPERIMENT: "Предложение эксперимента", NO_CHANGE: "Без изменений"};
const targets = {CONTENT_ITEM: "Контент", PUBLICATION_PLAN: "Медиаплан", CAMPAIGN_STRATEGY: "Стратегия кампании", CAMPAIGN: "Кампания"};
const evidenceLabels = {publication: "Публикация", content_version: "Версия контента", metrics_snapshot: "Снимок метрик", marketing_feedback: "Обратная связь"};
const statuses = {PROPOSED: "Ожидает решения", APPROVED: "Принято", REJECTED: "Отклонено", APPLIED: "Применено", FAILED: "Ошибка"};

export function OptimizationProposalPanel({analysis, archived}: {analysis: FeedbackAnalysis; archived: boolean}) {
  const [proposal, setProposal] = useState<OptimizationProposal | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [applying, setApplying] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  useEffect(() => {
    let active = true;
    void optimizationApi.list(analysis.campaign_id).then(rows => {if (active) setProposal(rows.find(row => row.feedback_analysis_id === analysis.id) ?? null);}).catch(reason => {if (active) setError(reason instanceof Error ? reason : new Error("Не удалось загрузить рекомендации."));});
    return () => {active = false;};
  }, [analysis.campaign_id, analysis.id]);
  async function decide(id: string, decision: "approve" | "reject") {
    if (!proposal || pending.current) return;
    pending.current = true;
    setBusy(true); setError(null);
    try {await optimizationApi[decision](id); setProposal(await optimizationApi.get(proposal.id));}
    catch (reason) {setError(reason instanceof Error ? reason : new Error("Не удалось сохранить решение."));}
    finally {pending.current = false; setBusy(false);}
  }
  async function apply(id: string) {
    if (!proposal || pending.current) return;
    pending.current = true; setBusy(true); setError(null);
    try {await optimizationApi.apply(id, comment.trim() || null); setProposal(await optimizationApi.get(proposal.id)); setApplying(null); setComment("");}
    catch (reason) {setError(reason instanceof Error ? reason : new Error("Не удалось применить рекомендацию."));}
    finally {pending.current = false; setBusy(false);}
  }
  if (!proposal) return error ? <FriendlyError error={error} /> : null;
  return <section id={`proposal-${proposal.id}`} aria-label="Рекомендации к изменениям">
    <h3>Рекомендации к изменениям</h3>
    <p className="notice">Принятие рекомендации фиксирует решение. Применение отдельно создаёт новый workflow или черновик с последующим согласованием.</p>
    <p>Стратегия версии {proposal.strategy_version} · Статус предложения: {proposal.status}</p>
    {Boolean(error) && <FriendlyError error={error} />}
    <div className="optimization-actions">{proposal.actions.map(action => <article id={`action-${action.id}`} className="card optimization-action" key={action.id} aria-label={types[action.type]}>
      <h4>{types[action.type]}</h4>
      <p>{targets[action.target_entity_type]}: {action.target_title ?? "Связанный объект"} <small title={action.target_entity_id}>{action.target_entity_id.slice(0, 8)}</small>{action.target_version_id && <span title={action.target_version_id}> · версия {action.target_version_number ? `v${action.target_version_number}` : action.target_version_id.slice(0, 8)}</span>}</p>
      <p><strong>Причина:</strong> {action.reason}</p><p><strong>Ожидаемый эффект:</strong> {action.expected_effect}</p><p><strong>Приоритет:</strong> {action.priority}</p>
      <p><strong>Доказательства из принятого анализа:</strong></p>
      {action.evidence_refs.length ? <ul>{action.evidence_refs.map(ref => <li key={`${ref.type}:${ref.id}`}>{evidenceLabels[ref.type]}: {ref.id}</li>)}</ul> : <p>Доказательств недостаточно для уверенного изменения.</p>}
      <p><strong>Статус:</strong> {statuses[action.status]}</p>
      <div className="actions"><button disabled={busy || archived || action.status !== "PROPOSED"} onClick={() => decide(action.id, "approve")}>Принять</button><button className="secondary" disabled={busy || archived || action.status !== "PROPOSED"} onClick={() => decide(action.id, "reject")}>Отклонить</button></div>
      {action.status === "APPROVED" && action.type === "NO_CHANGE" && <p>Изменения не требуются.</p>}
      {action.status === "APPROVED" && action.type === "EXPERIMENT" && (action.experiment_spec ? <>
        <button disabled={busy || archived} onClick={() => setApplying(action.id)}>Создать эксперимент</button>
        {applying === action.id && <ExperimentCreateForm action={action} campaignId={analysis.campaign_id} archived={archived} onCreated={async () => {setProposal(await optimizationApi.get(proposal.id)); setApplying(null);}} />}
      </> : <p>Для этой старой рекомендации отсутствуют структурированные параметры эксперимента. Сформируйте новый анализ на актуальных данных.</p>)}
      {action.status === "APPROVED" && ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"].includes(action.type) && <>
        <button disabled={busy || archived} onClick={() => {setApplying(action.id); setComment("");}}>Применить</button>
        {applying === action.id && <div role="group" aria-label="Подтверждение применения">
          <p className="notice">Применение создаст новый workflow/черновик. Текущая утверждённая версия останется действующей до отдельного согласования.</p>
          <label htmlFor={`apply-comment-${action.id}`}>Комментарий человека{action.type === "CONTENT_REVISION" ? " (обязательно)" : ""}</label>
          <textarea id={`apply-comment-${action.id}`} maxLength={2000} value={comment} onChange={event => setComment(event.target.value)} disabled={busy} />
          <button disabled={busy || archived || (action.type === "CONTENT_REVISION" && !comment.trim())} onClick={() => apply(action.id)}>Подтвердить применение</button>
          <button className="secondary" disabled={busy} onClick={() => setApplying(null)}>Отмена</button>
        </div>}
      </>}
      {action.status === "APPLIED" && action.applied_artifact && <div>
        <p>Создан: {action.applied_artifact.artifact_type} · Статус workflow: {action.applied_artifact.status}</p>
        <a href={action.applied_artifact.href}>Открыть {action.applied_artifact.artifact_type === "TASK" ? "задачу" : action.applied_artifact.artifact_type === "MARKETING_EXPERIMENT" ? "эксперимент" : "медиаплан"}</a>
        {action.applied_artifact.error_message && <FriendlyError error={new Error(action.applied_artifact.error_message)} />}
      </div>}
      <OptimizationProvenanceView key={`${action.id}:${action.status}`} actionId={action.id} />
    </article>)}</div>
  </section>;
}
