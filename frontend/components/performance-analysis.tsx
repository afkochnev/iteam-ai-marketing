"use client";

import type { FeedbackAnalysis } from "@/lib/api";

export function PerformanceAnalysisDetails({analysis, archived, retry}: {analysis: FeedbackAnalysis; archived: boolean; retry: () => void}) {
  const quality = analysis.input_snapshot.data_quality as {published_publication_count?: number; publications_with_metrics?: number; human_feedback_count?: number; raw_metric_coverage?: Record<string, number>} | undefined;
  const metrics = analysis.input_snapshot.publication_metrics_snapshot_ids as string[] | undefined;
  const feedback = analysis.input_snapshot.marketing_feedback_ids as string[] | undefined;
  return <>
    <p>{analysis.trigger_source === "AUTOMATIC" ? "AUTOMATIC · Сформирован автоматически" : analysis.trigger_source === "MANUAL" ? "MANUAL · По запросу" : "Исторический анализ"} · Стратегия v{analysis.strategy_version}</p>
    <p>Evidence: {analysis.evidence_fingerprint?.slice(0, 12) ?? "не указан"} · {metrics?.length ?? 0} метрик / {feedback?.length ?? 0} отзывов</p>
    {quality && <><p>Покрытие: {quality.publications_with_metrics ?? 0} / {quality.published_publication_count ?? 0} публикаций; отзывов: {quality.human_feedback_count ?? 0}</p>{Object.entries(quality.raw_metric_coverage ?? {}).map(([metric, ratio]) => <span key={metric}>{metric}: {Math.round(ratio * 100)}% · </span>)}</>}
    {(analysis.interpretations ?? []).map((item, index) => <p key={index}>Интерпретация: {String(item.interpretation ?? "")} · уверенность: {String(item.confidence ?? "")} · ограничения: {Array.isArray(item.limitations) ? item.limitations.join("; ") : ""}</p>)}
    {analysis.recommendations.map((item, index) => <p key={index}>Рекомендация: {String(item.recommendation ?? "")}</p>)}
    {analysis.experiment_ideas.map((item, index) => <p key={index}>Гипотеза: {String(item.hypothesis ?? "")} · изменение: {String(item.proposed_change ?? "")} · метрика: {String(item.success_metric ?? "")} · минимум наблюдений: {String(item.minimum_observation_requirement ?? "не задан")}</p>)}
    <p>Автоматический анализ требует отдельного решения человека. Рекомендации не принимаются и не применяются автоматически.</p>
    {analysis.status === "FAILED" && !archived && <button className="secondary" onClick={retry}>Повторить анализ</button>}
  </>;
}
