import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PerformanceAnalysisDetails } from "@/components/performance-analysis";
import type { FeedbackAnalysis } from "@/lib/api";

const analysis: FeedbackAnalysis = {id: "a", campaign_id: "c", status: "DRAFT", strategy_version: 4, summary: "Evidence", input_snapshot: {publication_metrics_snapshot_ids: ["m"], marketing_feedback_ids: ["f"], data_quality: {published_publication_count: 2, publications_with_metrics: 1, human_feedback_count: 1, raw_metric_coverage: {views: .5, clicks: 0}}}, findings: [], recommendations: [{recommendation: "Human decision"}], interpretations: [{interpretation: "Possible pattern", confidence: "low", limitations: ["No causality"]}], experiment_ideas: [{hypothesis: "Try format", success_metric: "VIEWS"}], limitations: [], task_id: "t", evidence_fingerprint: "0123456789abcdef", trigger_source: "AUTOMATIC", agent_run_id: null, generated_at: null, reviewed_at: null, reviewed_by_user_id: null};

describe("Performance analysis", () => {
  it("separates interpretation and hypotheses, shows frozen coverage and human gate", () => {
    render(<PerformanceAnalysisDetails analysis={analysis} archived={false} retry={vi.fn()} />);
    expect(screen.getByText(/AUTOMATIC/)).toBeInTheDocument();
    expect(screen.getByText(/0123456789ab/)).toBeInTheDocument();
    expect(screen.getByText(/Покрытие: 1 \/ 2/)).toBeInTheDocument();
    expect(screen.getByText(/Интерпретация: Possible pattern/)).toBeInTheDocument();
    expect(screen.getByText(/Гипотеза: Try format/)).toBeInTheDocument();
    expect(screen.getByText(/требует отдельного решения человека/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
  it("retries failed analysis and hides retry when archived", () => {
    const retry = vi.fn();
    const {rerender} = render(<PerformanceAnalysisDetails analysis={{...analysis, status: "FAILED"}} archived={false} retry={retry} />);
    fireEvent.click(screen.getByRole("button", {name: "Повторить анализ"}));
    expect(retry).toHaveBeenCalledOnce();
    rerender(<PerformanceAnalysisDetails analysis={{...analysis, status: "FAILED"}} archived={true} retry={retry} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
  it("keeps historical null fields readable", () => {
    render(<PerformanceAnalysisDetails analysis={{...analysis, evidence_fingerprint: null, trigger_source: null, interpretations: undefined}} archived={false} retry={vi.fn()} />);
    expect(screen.getByText(/Исторический анализ/)).toBeInTheDocument();
    expect(screen.getByText(/Evidence: не указан/)).toBeInTheDocument();
  });
});
