"use client";
import { PerformanceAnalysisDetails } from "@/components/performance-analysis";
import { CampaignKPIBlock, PublicationPerformanceTable } from "@/components/campaign-kpis";

import { MarketingExperimentsPanel } from "@/components/marketing-experiments";
import { OptimizationProposalPanel } from "@/components/optimization-proposal";

import Link from "@/components/hash-link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { AutonomousPostProgress } from "@/components/autonomous-post-progress";
import { useHashTarget } from "@/components/use-hash-target";
import { PublicationDeliveryNotice } from "@/components/publication-delivery-notice";
import { StatusBadge } from "@/components/status-badge";
import { activitiesApi, agentRunsApi, agentsApi, approvalsApi, campaignsApi, contentApi, feedbackApi, metricsApi, publicationPlansApi, publicationsApi, tasksApi, type Activity, type AgentListItem, type AgentRun, type Approval, type Campaign, type CampaignPerformance, type CampaignWorkspace, type ContentListItem, type FeedbackAnalysis, type MarketingFeedback, type Publication, type PublicationCalendarItem, type PublicationPlan, type TaskListItem, type WorkspacePlanItem } from "@/lib/api";
import { FriendlyError } from "@/components/friendly-error";
import { ApiError } from "@/lib/api";
import { CAMPAIGN_STATUS_LABELS, formatDate, formatDateTime } from "@/lib/campaigns";
import { activityLabel, CONTENT_STATUS_LABELS, PLAN_STATUS_LABELS, PUBLICATION_STATUS_LABELS } from "@/lib/presentation";
import { TASK_STATUS_LABELS, TASK_TYPE_LABELS } from "@/lib/tasks";

export type CampaignSection = "overview" | "strategy" | "content" | "plan" | "publications" | "performance" | "tasks" | "activity";
export default function CampaignSections({ section = "overview" }: { section?: CampaignSection }) {
  const { id } = useParams<{ id: string }>(); const router = useRouter(); const { user, loading: authLoading } = useAuth();
  const [campaign, setCampaign] = useState<Campaign | null>(null); const [error, setError] = useState("");
  const [workspace, setWorkspace] = useState<CampaignWorkspace | null>(null);
  const [agents, setAgents] = useState<AgentListItem[]>([]);
  const [planGenerationState, setPlanGenerationState] = useState<"idle" | "queued" | "complete" | "failed">("idle");
  const [planGenerationError, setPlanGenerationError] = useState<unknown>(null);
  const [planGenerationRun, setPlanGenerationRun] = useState<AgentRun | null>(null);
  const [generatedPlanId, setGeneratedPlanId] = useState<string | null>(null);
  const [postBusyItemId, setPostBusyItemId] = useState<string | null>(null);
  const [postJobs, setPostJobs] = useState<Record<string, { taskId: string; runId?: string; contentId?: string; status: string }>>({});
  const [actionNotice, setActionNotice] = useState("");
  const [tasks, setTasks] = useState<TaskListItem[]>([]); const [approvals, setApprovals] = useState<Approval[]>([]); const [contents, setContents] = useState<ContentListItem[]>([]); const [publications, setPublications] = useState<Publication[]>([]); const [calendarItems, setCalendarItems] = useState<PublicationCalendarItem[]>([]); const [activities, setActivities] = useState<Activity[]>([]); const [action, setAction] = useState<"revision" | "reject" | null>(null); const [comment, setComment] = useState(""); const [publicationPollingMessage, setPublicationPollingMessage] = useState("");
  const [performance, setPerformance] = useState<CampaignPerformance | null>(null);
  const [feedback, setFeedback] = useState<MarketingFeedback[]>([]); const [analyses, setAnalyses] = useState<FeedbackAnalysis[]>([]);
  const [publicationPlans, setPublicationPlans] = useState<PublicationPlan[]>([]);
  const [publicationBusy, setPublicationBusy] = useState<string | null>(null);
  const [metricsBusy, setMetricsBusy] = useState<string | null>(null);
  const publicationPollingStartedAt = useRef<number | null>(null);
  const load = useCallback(() => { void Promise.all([campaignsApi.get(id), campaignsApi.workspace(id), tasksApi.list({ campaign_id: id }), approvalsApi.list({ object_type: "CAMPAIGN_STRATEGY", object_id: id }), contentApi.list({ campaign_id: id }), activitiesApi.list(id).catch(() => [])]).then(([campaignValue, workspaceValue, taskRows, approvalRows, contentRows, activityRows]) => { setCampaign(campaignValue); window.dispatchEvent(new CustomEvent("campaign-workspace-updated", {detail: id})); setWorkspace(workspaceValue); setTasks(taskRows); setApprovals(approvalRows); setContents(contentRows); setActivities(activityRows); }).catch((reason: Error) => setError(reason.message)); void agentsApi.list().then(setAgents).catch(() => setAgents([])); void publicationsApi.listCampaign(id).then(setPublications).catch(() => setPublications([])); const to = new Date(); const from = new Date(to.getTime() - 180 * 24 * 60 * 60 * 1000); const calendarFrom = new Date(to.getTime() - 7 * 24 * 60 * 60 * 1000); const calendarTo = new Date(to.getTime() + 60 * 24 * 60 * 60 * 1000); const calendarRequest = publicationsApi.calendar?.(id, calendarFrom.toISOString(), calendarTo.toISOString()); if (calendarRequest) void calendarRequest.then(setCalendarItems).catch(() => setCalendarItems([])); void metricsApi.campaign(id, from.toISOString(), to.toISOString()).then(setPerformance).catch(() => setPerformance(null)); }, [id]);
  const loadFeedback = useCallback(() => { void feedbackApi.list(id).then(setFeedback).catch(() => setFeedback([])); void feedbackApi.analyses(id).then(setAnalyses).catch(() => setAnalyses([])); }, [id]);
  useHashTarget(campaign ? publicationPlans : null);
  const loadPlans = useCallback(() => { void publicationPlansApi.list(id).then(setPublicationPlans).catch(() => setPublicationPlans([])); }, [id]);
  useEffect(() => { if (!authLoading && !user) { router.replace("/login"); return; } if (user) { load(); loadFeedback(); loadPlans(); } }, [authLoading, user, router, load, loadFeedback, loadPlans]);
  useEffect(() => { if (campaign?.status !== "PLANNING") return; const timer = window.setInterval(load, 3000); return () => window.clearInterval(timer); }, [campaign?.status, load]);
  const hasPublishingPublication = publications.some((publication) => publication.status === "PUBLISHING");
  useEffect(() => {
    if (!hasPublishingPublication) { publicationPollingStartedAt.current = null; return; }
    if (publicationPollingStartedAt.current === null) publicationPollingStartedAt.current = Date.now();
    const timer = window.setInterval(() => {
      if (Date.now() - (publicationPollingStartedAt.current ?? Date.now()) >= 30_000) {
        window.clearInterval(timer); setPublicationPollingMessage("Статус доставки всё ещё подтверждается."); return;
      }
      void publicationsApi.listCampaign(id).then(setPublications).catch(() => undefined);
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [hasPublishingPublication, id]);
  async function archive() { if (!window.confirm("Архивировать кампанию? После архивации редактирование будет недоступно.")) return; try { setCampaign(await campaignsApi.archive(id)); window.dispatchEvent(new CustomEvent("campaign-workspace-updated", {detail: id})); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось архивировать кампанию."); } }
  async function generate() { try { await campaignsApi.generateStrategy(id); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сформировать стратегию."); } }
  async function prepareStrategyRevision() { try { await campaignsApi.prepareStrategyRevision(id); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось начать подготовку новой версии стратегии."); } }
  async function approve() { try { await campaignsApi.approveStrategy(id); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось согласовать стратегию."); } }
  async function submitDecision() { if (!action || !comment.trim()) { setError("Комментарий обязателен."); return; } try { if (action === "revision") await campaignsApi.requestStrategyRevision(id, comment); else await campaignsApi.rejectStrategy(id, comment); setAction(null); setComment(""); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить решение."); } }
  async function preparePublication(item: ContentListItem) { setPublicationBusy(item.id); try { if (item.publication_plan_item_id) { const scheduled = await publicationsApi.scheduleContent(item.id); setPublications((rows) => [scheduled, ...rows.filter((row) => row.id !== scheduled.id)]); load(); return; } const detail = await contentApi.get(item.id); if (!detail.approved_version_id || !item.channel) throw new Error("У материала отсутствует согласованная версия или канал."); await publicationsApi.create({ content_item_id: item.id, content_version_id: detail.approved_version_id, channel: item.channel }); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось подготовить публикацию."); } finally { setPublicationBusy(null); } }
  async function approvePublication(publicationId: string) { try { await publicationsApi.approve(publicationId); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось согласовать публикацию."); } }
  async function schedulePublication(publicationId: string) { const value = window.prompt("Дата и время публикации (ISO или локальное время браузера)"); if (!value) return; const parsed = new Date(value); if (Number.isNaN(parsed.getTime())) { setError("Укажите корректные дату и время."); return; } try { await publicationsApi.schedule(publicationId, parsed.toISOString()); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось назначить публикацию."); } }
  async function cancelPublication(publicationId: string) { if (!window.confirm("Отменить публикацию?")) return; try { await publicationsApi.cancel(publicationId); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось отменить публикацию."); } }
  async function publishNow(publicationId: string) { if (!window.confirm("Поставить exact согласованную версию в очередь для отправки сейчас?")) return; try { await publicationsApi.publishNow(publicationId); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось поставить публикацию в очередь."); } }
  async function retryPublication(publicationId: string) { try { await publicationsApi.retry(publicationId); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось повторить публикацию."); } }
  async function reconcilePublished(publicationId: string) { const externalId = window.prompt("Внешний ID публикации"); if (!externalId?.trim()) return; const externalUrl = window.prompt("Внешняя ссылка (необязательно)") ?? ""; const publishedAt = window.prompt("Время внешней публикации ISO (необязательно)") ?? ""; const note = window.prompt("Комментарий (необязательно)") ?? ""; try { await publicationsApi.reconcilePublished(publicationId, { external_id: externalId.trim(), external_url: externalUrl || undefined, published_at: publishedAt || undefined, note: note || undefined }); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось подтвердить публикацию."); } }
  async function reconcileNotPublished(publicationId: string) { if (!window.confirm("Подтвердить, что внешней публикации нет?")) return; const note = window.prompt("Комментарий (необязательно)") ?? ""; try { await publicationsApi.reconcileNotPublished(publicationId, note || undefined); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось завершить сверку."); } }
  async function addManualMetrics(publicationId: string) { const rawViews = window.prompt("Просмотры (необязательно)"); if (rawViews === null) return; const views = rawViews.trim() === "" ? null : Number(rawViews); if (views !== null && (!Number.isInteger(views) || views < 0)) { setError("Просмотры должны быть целым неотрицательным числом."); return; } setMetricsBusy(publicationId); try { await publicationsApi.addManualMetrics(publicationId, { observed_at: new Date().toISOString(), views }); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить метрики."); } finally { setMetricsBusy(null); } }
  async function addFeedback() { const comment = window.prompt("Обратная связь"); if (!comment?.trim()) return; try { await feedbackApi.create(id, { category: "OTHER", comment }); await loadFeedback(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить обратную связь."); } }
  async function generateFeedbackAnalysis() { try { await feedbackApi.generate(id); await loadFeedback(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сформировать выводы."); } }
  async function reviewFeedbackAnalysis(analysisId: string, decision: "accept" | "reject") { try { if (decision === "accept") await feedbackApi.accept(analysisId); else await feedbackApi.reject(analysisId); await loadFeedback(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить решение."); } }
  async function generatePublicationPlan() {
    const start = new Date();
    start.setHours(9, 0, 0, 0);
    const end = new Date(start);
    end.setDate(end.getDate() + 13);
    end.setHours(18, 0, 0, 0);
    setPlanGenerationState("queued");
    setPlanGenerationError(null);
    setActionNotice("");
    try {
      const plan = await publicationPlansApi.generate(id, {
        planning_horizon_start: start.toISOString(),
        planning_horizon_end: end.toISOString(),
        channels: ["TELEGRAM", "VK"],
        total_items: 6,
      });
      setGeneratedPlanId(plan.id);
      setPublicationPlans(await publicationPlansApi.list(id));
      if (plan.generated_by_agent_run_id) {
        const run = await agentRunsApi.get(plan.generated_by_agent_run_id);
        setPlanGenerationRun(run);
        if (run.status === "COMPLETED") setPlanGenerationState("complete");
        if (run.status === "FAILED" || run.status === "CANCELLED") {
          setPlanGenerationState("failed");
          setPlanGenerationError(new ApiError(
            run.error_message ?? "Генерация медиаплана остановилась.",
            500,
            run.error_code ?? undefined,
          ));
        }
      } else {
        setPlanGenerationState("complete");
      }
      void load();
    } catch (reason) {
      setPlanGenerationState("failed");
      setPlanGenerationError(reason);
      setActionNotice("");
    }
  }
  useEffect(() => { if (!planGenerationRun || !["QUEUED", "RUNNING"].includes(planGenerationRun.status)) return; const timer = window.setInterval(() => { void agentRunsApi.get(planGenerationRun.id).then((run) => { setPlanGenerationRun(run); if (run.status === "COMPLETED") { setPlanGenerationState("complete"); void load(); void loadPlans(); } else if (run.status === "FAILED" || run.status === "CANCELLED") { setPlanGenerationState("failed"); setPlanGenerationError(new ApiError(run.error_message ?? "Генерация медиаплана остановилась.", 500, run.error_code ?? undefined)); setActionNotice(""); } }).catch(() => undefined); }, 2500); return () => window.clearInterval(timer); }, [planGenerationRun, load, loadPlans]);
  async function createPostForPlanItem(item: WorkspacePlanItem, planId: string) { const manager = agents.find((agent) => agent.slug === "smm_manager" && agent.status === "ACTIVE"); if (!manager) { setPlanGenerationError(new ApiError("SMM Manager не найден или неактивен.", 409, "AGENT_INACTIVE")); return; } setPostBusyItemId(item.id); setPlanGenerationError(null); setActionNotice(""); try { const task = await tasksApi.create({ campaign_id: id, task_type: "CREATE_SOCIAL_POSTS", title: `Создать ${item.channel}-пост: «${item.topic}»`, description: `Создать один пост по утверждённому пункту медиаплана №${item.position}. Источник: ${item.source_content_item_title ?? "связь не зафиксирована"}, утверждённая версия v${item.source_version_number ?? "—"}.`, assigned_agent_id: manager.id, priority: "NORMAL", requires_approval: true, input_data: { publication_plan_id: planId, publication_plan_item_id: item.id, source_content_item_id: item.source_content_item_id, source_content_version_id: item.source_content_version_id } }); setPostJobs((current) => ({ ...current, [item.id]: { taskId: task.id, status: "READY" } })); setActionNotice("Задача создана и ожидает автоматического запуска"); await load(); } catch (reason) { setPlanGenerationError(reason); } finally { setPostBusyItemId(null); } }
  async function transitionPlan(planId: string, actionName: "submit" | "approve" | "reject") { try { await publicationPlansApi[actionName](planId); await loadPlans(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось изменить план публикаций."); } }
  async function editPlanItem(plan: PublicationPlan, itemId: string) { const item = plan.items.find((candidate) => candidate.id === itemId); if (!item) return; const topic = window.prompt("Тема", item.topic); if (topic === null) return; const angle = window.prompt("Угол", item.angle); if (angle === null) return; const purpose = window.prompt("Цель", item.purpose); if (purpose === null) return; const format = window.prompt("Формат", item.format); if (format === null) return; const brief = window.prompt("Бриф", item.message_brief); if (brief === null) return; try { await publicationPlansApi.updateItem(plan.id, item.id, { topic, angle, purpose, format, message_brief: brief }); await loadPlans(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить пункт плана."); } }
  async function removePlanItem(plan: PublicationPlan, itemId: string) { if (!window.confirm("Удалить пункт плана?")) return; try { await publicationPlansApi.removeItem(plan.id, itemId); await loadPlans(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось удалить пункт плана."); } }
  async function movePlanItem(plan: PublicationPlan, index: number, direction: -1 | 1) { const target = index + direction; if (target < 0 || target >= plan.items.length) return; const ids = plan.items.map((item) => item.id); [ids[index], ids[target]] = [ids[target], ids[index]]; try { await publicationPlansApi.reorder(plan.id, ids); await loadPlans(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось изменить порядок пунктов."); } }
  async function addPlanItem(plan: PublicationPlan) { const source = contents.find((item) => item.content_type === "ARTICLE" && item.status === "APPROVED" && !plan.items.some((planned) => planned.source_content_item_id === item.id)); if (!source) { setError("Нет доступной утверждённой статьи для нового пункта."); return; } const detail = await contentApi.get(source.id); if (!detail.approved_version_id) { setError("У статьи нет утверждённой версии."); return; } const scheduled = window.prompt("Дата/время ISO", new Date(plan.planning_horizon_start).toISOString()); if (!scheduled) return; const channel = window.prompt("Канал TELEGRAM или VK", "TELEGRAM"); if (channel !== "TELEGRAM" && channel !== "VK") { setError("Поддерживаются только TELEGRAM и VK."); return; } try { await publicationPlansApi.addItem(plan.id, { scheduled_at: scheduled, channel, source_content_version_id: detail.approved_version_id, topic: window.prompt("Тема", source.title) ?? source.title, angle: window.prompt("Угол", "Практический управленческий вывод") ?? "Практический управленческий вывод", purpose: window.prompt("Цель", "Помочь применить вывод статьи") ?? "Помочь применить вывод статьи", format: window.prompt("Формат", "expert_observation") ?? "expert_observation", message_brief: window.prompt("Бриф", "Краткий редакционный бриф") ?? "Краткий редакционный бриф" }); await loadPlans(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось добавить пункт плана."); } }
  if (authLoading || (!campaign && !error)) return <section><p role="status">Загружаем кампанию…</p></section>;
  if (!campaign) return <section className="page-section"><p role="alert" className="error">{error}</p><Link href="/campaigns">К кампаниям</Link></section>;

  const archived = campaign.status === "ARCHIVED";
  const articles = contents.filter((item) => item.content_type === "ARTICLE");
  const socialPosts = contents.filter((item) => item.content_type === "SOCIAL_POST");
  const socialPacks = contents.filter((item) => item.content_type === "SOCIAL_POST_PACK");
  const displayAttentionTasks = workspace?.attention_tasks ?? [];
  const getPublication = (item: ContentListItem) => publications.find((row) => row.content_item_id === item.id && row.status !== "CANCELLED" &&
    (item.approved_version_id ? row.content_version_id === item.approved_version_id : !item.current_version_id || row.content_version_id === item.current_version_id))
    ?? publications.find((row) => row.content_item_id === item.id && !["CANCELLED", "PUBLISHED"].includes(row.status));
  const upcomingPublications = publications.filter((item) => item.status === "SCHEDULED" && item.scheduled_at && !item.is_overdue)
    .sort((a, b) => new Date(a.scheduled_at ?? 0).getTime() - new Date(b.scheduled_at ?? 0).getTime()).slice(0, 10);
  const calendarUpcoming = calendarItems.filter((item) => item.status === "SCHEDULED" && item.scheduled_at && !item.is_overdue)
    .sort((a, b) => new Date(a.scheduled_at ?? 0).getTime() - new Date(b.scheduled_at ?? 0).getTime()).slice(0, 10);
  const publishedItems = publications.filter((item) => item.status === "PUBLISHED")
    .sort((a, b) => new Date(b.published_at ?? b.updated_at).getTime() - new Date(a.published_at ?? a.updated_at).getTime());
  const calendarGroups = calendarItems.reduce<Record<string, PublicationCalendarItem[]>>((groups, item) => {
    const timestamp = item.scheduled_at ?? item.published_at;
    if (!timestamp) return groups;
    const date = formatDate(timestamp);
    (groups[date] ??= []).push(item);
    return groups;
  }, {});
  const activePlans = publicationPlans.filter((plan) => plan.status !== "ARCHIVED");
  const currentPlan = publicationPlans.find((plan) => plan.id === generatedPlanId) ??
    activePlans.find((plan) => plan.status === "APPROVED") ??
    activePlans.find((plan) => plan.status === "WAITING_APPROVAL") ??
    activePlans.find((plan) => plan.status === "DRAFT") ??
    activePlans.find((plan) => plan.status === "REJECTED") ??
    publicationPlans[0];
  const otherPlans = publicationPlans.filter((plan) => plan.id !== currentPlan?.id);
  const currentWorkspacePlan = currentPlan?.id === workspace?.publication_plan?.id
    ? workspace.publication_plan
    : workspace?.other_plans.find((plan) => plan.id === currentPlan?.id) ?? workspace?.publication_plan ?? null;
  const planWorkspaceById = new Map<string, CampaignWorkspace["publication_plan"]>([
    ...(workspace?.publication_plan ? [[workspace.publication_plan.id, workspace.publication_plan] as const] : []),
    ...(workspace?.other_plans.map((plan) => [plan.id, plan] as const) ?? []),
  ]);
  const pendingStrategyApproval = approvals.find((approval) => approval.status === "PENDING");
  const strategyToReview = (pendingStrategyApproval?.subject_snapshot ?? campaign.strategy) as Campaign["strategy"];
  const changeState = workspace?.change_state;

  const renderPublication = (item: ContentListItem) => {
    const publication = getPublication(item);
    return <article key={item.id} id={publication ? `delivery-publication-${publication.id}` : undefined} className="content-card">
      <div className="card-heading">
        <div><h3><Link href={`/content/${item.id}`}>{item.title}</Link></h3><p className="muted">{item.channel ?? item.plan_channel ?? "Канал не задан"}{item.source_content_item_title ? ` · Источник: ${item.source_content_item_title}` : item.parent_content_item_id ? ` · Пакет: ${socialPacks.find((pack) => pack.id === item.parent_content_item_id)?.title ?? "соцсети"}` : ""}</p></div>
        <StatusBadge label={CONTENT_STATUS_LABELS[item.status]} tone={item.status === "APPROVED" ? "success" : item.status === "REJECTED" ? "danger" : "neutral"} />
      </div>
      {item.publication_plan_item_id && <p className="plan-context">План: {item.plan_channel ?? item.channel ?? "Канал не указан"} · {item.plan_scheduled_at ? formatDateTime(item.plan_scheduled_at) : "Дата уточняется"}</p>}
      {publication ? <>
        {publication.is_overdue && <p role="alert">Плановое время прошло; автоматическая отправка остановлена до решения. Перенос плановой публикации требует изменения медиаплана.</p>}
        {publication.status === "SCHEDULED" && publication.scheduled_at && <p>Публикация запланирована на {formatDateTime(publication.scheduled_at)} · {publication.channel}</p>}
        <p>Статус публикации: <StatusBadge label={PUBLICATION_STATUS_LABELS[publication.status]} status={publication.status} />{publication.status !== "SCHEDULED" && publication.scheduled_at ? ` · ${formatDateTime(publication.scheduled_at)}` : ""}</p>
        {publication.content_version_id !== item.current_version_id && <p>Публикация привязана к другой версии. <Link href={`/content/${item.id}#publication`}>Проверить версии и разрешённую замену</Link></p>}
        {publication.status === "SCHEDULED" && <PublicationDeliveryNotice providerEnabled={publication.provider_enabled} />}
        {publication.external_url && <p><a href={publication.external_url} target="_blank" rel="noreferrer">Открыть публикацию</a></p>}
        {publication.reconciliation_required && <div className="actions"><p role="alert">Нужно подтвердить результат отправки.</p><button disabled={archived} onClick={() => reconcilePublished(publication.id)}>Подтвердить публикацию</button><button disabled={archived} className="secondary" onClick={() => reconcileNotPublished(publication.id)}>Подтвердить отсутствие публикации</button></div>}
        {publication.status === "PUBLISHING" && <p role="status">Отправка выполняется; статус обновится автоматически.</p>}
        {["DRAFT", "WAITING_APPROVAL"].includes(publication.status) && <button disabled={archived} onClick={() => approvePublication(publication.id)}>Согласовать публикацию</button>}
        {((!item.publication_plan_item_id && publication.status === "APPROVED") || (publication.status === "SCHEDULED" && publication.is_overdue)) && publication.provider_enabled !== false && <button disabled={archived} onClick={() => publishNow(publication.id)}>Опубликовать сейчас</button>}
        {!item.publication_plan_item_id && ["APPROVED", "SCHEDULED"].includes(publication.status) && <button disabled={archived} className="secondary" onClick={() => schedulePublication(publication.id)}>Назначить / перенести</button>}
        {publication.status === "FAILED" && publication.retry_allowed && <button disabled={archived} onClick={() => retryPublication(publication.id)}>Повторить</button>}
        {!publication.reconciliation_required && !["PUBLISHED", "CANCELLED", "PUBLISHING"].includes(publication.status) && <button disabled={archived} className="secondary" onClick={() => cancelPublication(publication.id)}>Отменить</button>}
      </> : <>
        <p>Публикация ещё не назначена.</p>
        {item.status === "APPROVED" && !archived && <button disabled={archived || publicationBusy === item.id} onClick={() => preparePublication(item)}>{item.publication_plan_item_id ? "Запланировать публикацию" : "Подготовить публикацию"}</button>}
      </>}
    </article>;
  };

  const renderPlanCard = (plan: PublicationPlan) => (
    <article className="plan-card" key={plan.id} id={`publication-plan-${plan.id}`} tabIndex={-1}>
      {plan.optimization_action_id && <p>Источник: <Link href={`/campaigns/${id}/performance#action-${plan.optimization_action_id}`}>Принятая рекомендация к изменению</Link></p>}
      <div className="card-heading"><div><h3>План · {formatDate(plan.planning_horizon_start)} — {formatDate(plan.planning_horizon_end)}</h3><p>{plan.items.filter((item) => item.status === "PLANNED").length} активных пунктов · часовой пояс: {plan.timezone_policy}</p></div><StatusBadge label={PLAN_STATUS_LABELS[plan.status]} tone={plan.status === "APPROVED" ? "success" : "neutral"} /></div>
      <ol className="plan-list">{plan.items.filter((item) => item.status === "PLANNED").map((item, index) => {
        const planWorkspace = planWorkspaceById.get(plan.id);
        const workspaceItem = planWorkspace?.items.find((planned) => planned.id === item.id);
        const source = articles.find((article) => article.id === item.source_content_item_id);
        const childPostRef = workspaceItem?.social_posts[0];
        const childPost = childPostRef ? socialPosts.find((post) => post.id === childPostRef.id) : socialPosts.find((post) => post.publication_plan_item_id === item.id);
        const downstreamPublication = workspaceItem?.publications[0] ? publications.find((publication) => publication.id === workspaceItem.publications[0].id && publication.status !== "CANCELLED") : publications.find((publication) => publication.publication_plan_item_id === item.id && publication.status !== "CANCELLED") ?? (childPost ? getPublication(childPost) : undefined);
        return <li key={item.id} id={`plan-item-${item.id}`} tabIndex={-1} className="plan-item">
          <div className="plan-item-main"><strong>{formatDateTime(item.scheduled_at)}</strong><StatusBadge label={item.channel} /><h4>{item.topic}</h4>
            <p>{item.purpose} · {item.format}</p><p className="muted">{item.message_brief}</p>
            <p>Исходная статья: {source ? <Link href={`/content/${source.id}`}>{workspaceItem?.source_content_item_title ?? source.title}</Link> : "Связь не зафиксирована"} · версия {workspaceItem?.source_version_number ? `v${workspaceItem.source_version_number}` : "не определена"}</p>
            <p className="muted">Точная версия источника: {workspaceItem?.source_version_number ? `v${workspaceItem.source_version_number}` : "связь не подтверждена"} · {workspaceItem?.source_version_number && workspaceItem.source_claim_ids?.length ? `подтверждений: ${workspaceItem.source_claim_ids.length}` : "claim-связи не подтверждены"}</p>
            {workspaceItem?.source_version_number && (workspaceItem.source_support_summary ?? item.source_support_summary) && <p className="muted">Основание: {workspaceItem.source_support_summary ?? item.source_support_summary}</p>}
            {childPost ? <p>Пост: <Link href={`/content/${childPost.id}`}>{childPost.title}</Link></p> : <p className="muted">Пост для этого пункта ещё не создан.</p>}
            {workspaceItem && <ol className="workflow-pipeline" aria-label={`Цепочка пункта ${item.position}`}>{workspaceItem.pipeline.map((stage) => <li key={stage.label} className={`pipeline-stage pipeline-${stage.status}`}><span>{stage.href ? <Link href={stage.href}>{stage.label}</Link> : stage.label}</span><small>{stage.action_label ?? stage.status}</small></li>)}</ol>}
            {plan.status === "APPROVED" && workspaceItem && !workspaceItem.social_posts.length && !archived && (
              (postJobs[item.id]?.taskId ?? workspaceItem.post_action.existing_task_id)
                ? <AutonomousPostProgress taskId={(postJobs[item.id]?.taskId ?? workspaceItem.post_action.existing_task_id)!} onComplete={() => { void load(); void loadPlans(); }} />
                : workspaceItem.post_action.allowed
                  ? <button disabled={archived || postBusyItemId === item.id} onClick={() => void createPostForPlanItem(workspaceItem, plan.id)}>{postBusyItemId === item.id ? "Создаём задачу…" : "Создать пост"}</button>
                  : <div className="friendly-error"><p><strong>Пост пока нельзя создать.</strong></p><p><strong>Почему:</strong> {workspaceItem.post_action.reason ?? "Не выполнены условия запуска."}</p><p><strong>Что сделать:</strong> {workspaceItem.post_action.next_action ?? "Проверьте пункт медиаплана."}</p></div>
            )}
            {downstreamPublication ? <p>Дальше: {PUBLICATION_STATUS_LABELS[downstreamPublication.status]}{downstreamPublication.scheduled_at ? ` · ${formatDateTime(downstreamPublication.scheduled_at)}` : ""}</p> :
              childPost?.status === "APPROVED" && !archived ? <button disabled={archived || publicationBusy === childPost.id} onClick={() => preparePublication(childPost)}>Запланировать публикацию</button> :
                childPost && <p className="muted">Публикация станет доступна после утверждения поста.</p>}
          </div>
          {plan.status === "DRAFT" && !archived && <div className="plan-edit-actions"><button disabled={archived} className="secondary" onClick={() => editPlanItem(plan, item.id)}>Изменить</button><button disabled={archived} className="secondary" onClick={() => removePlanItem(plan, item.id)}>Убрать</button><button className="secondary" aria-label="Поднять пункт" disabled={archived || index === 0} onClick={() => movePlanItem(plan, index, -1)}>↑</button><button className="secondary" aria-label="Опустить пункт" disabled={archived || index === plan.items.filter((entry) => entry.status === "PLANNED").length - 1} onClick={() => movePlanItem(plan, index, 1)}>↓</button></div>}
        </li>;
      })}</ol>
      {plan.status === "DRAFT" && <div className="actions"><button disabled={archived} onClick={() => transitionPlan(plan.id, "submit")}>Отправить план на согласование</button><button disabled={archived} className="secondary" onClick={() => addPlanItem(plan)}>Добавить пункт</button></div>}
      {plan.status === "REJECTED" && <button disabled={archived} onClick={() => publicationPlansApi.revise(plan.id).then(loadPlans).catch((reason: Error) => setError(reason.message))}>Вернуть в редактуру</button>}
      {plan.status === "WAITING_APPROVAL" && <div className="actions"><button disabled={archived} onClick={() => transitionPlan(plan.id, "approve")}>Утвердить план</button><button disabled={archived} className="secondary" onClick={() => transitionPlan(plan.id, "reject")}>Вернуть на доработку</button></div>}
      <p className="muted">Утверждение плана не создаёт посты и не отправляет публикации автоматически.</p>
    </article>
  );

  return <div className="campaign-section-content">
    {error && <p role="alert" className="error">{error}</p>}{actionNotice && <p role="status">{actionNotice}</p>}
    {hasPublishingPublication && publicationPollingMessage && <p role="status">{publicationPollingMessage}</p>}{Boolean(planGenerationError) && <FriendlyError error={planGenerationError} fallback="Не удалось выполнить действие" onRetry={!archived && planGenerationState === "failed" ? () => void generatePublicationPlan() : undefined} />}
    {section === "overview" && <>      <div className="hero-actions"><Link className="button-link" href={`/campaigns/${id}/director`}>Обсудить с Директором по маркетингу</Link><StatusBadge label={CAMPAIGN_STATUS_LABELS[campaign.status]} /><Link className="button-link" href="/publications">К календарю публикаций</Link>{!archived && <><Link className="button-link secondary" href={`/campaigns/${id}/edit`}>Внести изменения</Link><button className="secondary" onClick={archive}>Архивировать</button></>}</div>
    {workspace && <section className="campaign-director page-section" aria-labelledby="director-heading">
      <div className="director-main"><div><p className="eyebrow">Рекомендация системы</p><h2 id="director-heading">{workspace.director.next_step.title}</h2><p>{workspace.director.next_step.description}</p><Link className="button-link" href={workspace.director.next_step.href}>Перейти к следующему шагу</Link></div>
        <aside className="director-role"><p className="eyebrow">Marketing Director</p><h3>Директор по маркетингу</h3><p>Объясняет состояние кампании, показывает блокеры и предлагает следующий шаг. Не утверждает контент и не публикует материалы.</p></aside></div>
      <div className="summary-grid metric-cards">
        <div className="metric-card"><strong>{workspace.director.approved_article_count}/{workspace.director.article_count}</strong><span>Статьи утверждены</span></div>
        <a className="metric-card" href={`/campaigns/${id}/plan`}><strong>{workspace.director.plan_item_count}</strong><span>Пункты медиаплана</span><small>{workspace.director.plan_status ? PLAN_STATUS_LABELS[workspace.director.plan_status] : "Медиаплан ещё не создан"}</small></a>
        <div className="metric-card"><strong>{workspace.director.plan_items_with_posts}/{workspace.director.plan_item_count}</strong><span>Посты созданы</span><small>{workspace.director.plan_items_without_posts} ещё не созданы</small></div>
        <Link className="metric-card" href={`/campaigns/${id}/publications`}><strong>{workspace.director.scheduled_publication_count}</strong><span>Запланировано</span><small>{workspace.director.published_count} опубликовано</small></Link>
        <Link className="metric-card" href={`/campaigns/${id}/tasks`}><strong>{workspace.director.failed_task_count + workspace.director.blocked_task_count}</strong><span>Блокеры</span><small>{workspace.director.failed_task_count} ошибок · {workspace.director.blocked_task_count} заблокировано</small></Link>
        <Link className="metric-card" href={`/knowledge?campaign_id=${id}`}><strong>{workspace.knowledge.ready_item_count}</strong><span>Материалы базы знаний</span><small>{workspace.knowledge.has_current_strategy_pack ? "Исследование для версии стратегии готово" : "Нет актуального пакета исследования"}</small></Link>
      </div>
      <p className="muted">Хранитель знаний готовит источники для этой кампании. <Link href={`/knowledge?campaign_id=${id}`}>Открыть базу знаний и исследование</Link>.</p>
    </section>}
<section className="section-links" aria-label="Разделы кампании">{[["strategy","Стратегия"],["content","Контент"],["plan","Медиаплан"],["publications","Публикации"],["performance","Результаты и оптимизация"],["tasks","Задачи"]].map(([path,label]) => <Link className="section-card" key={path} href={`/campaigns/${id}/${path}`}>{label}<span>Открыть раздел →</span></Link>)}</section></>}
    <div className="campaign-workflow-fields">
    {section === "content" && <>
    <section className="page-section" aria-labelledby="content-heading">
      <div className="section-heading"><div><p className="eyebrow">Редакция</p><h2 id="content-heading">Контент кампании</h2><p className="muted">Статьи и посты показаны отдельно. Пакет постов — это источник, а не публикация.</p></div><Link className="button-link secondary" href={`/content?campaign_id=${id}`}>Открыть библиотеку</Link></div>
      <div className="content-columns">
        {changeState?.has_pending_strategic_changes && <p className="warning">Эти материалы созданы по предыдущей версии стратегии. Утверждённые ContentVersions остаются неизменными.</p>}
        <section aria-labelledby="articles-heading"><h3 id="articles-heading">Статьи <span className="count">{articles.length}</span></h3>
          {workspace?.articles.length ? <div className="article-assets content-card-stack">{workspace.articles.map((item) => { const linkedPlanItems = currentWorkspacePlan?.items.filter((planItem) => planItem.source_content_item_id === item.id) ?? []; const postsToCreate = Math.max(0, linkedPlanItems.length - linkedPlanItems.filter((planItem) => planItem.social_posts.length > 0).length); return <article className="article-asset card" key={item.id}><div className="card-heading"><h4><Link href={`/content/${item.id}`}>{item.title}</Link></h4><StatusBadge label={CONTENT_STATUS_LABELS[item.status]} status={item.status} /></div><p>{item.approved_version_number ? `Утверждена версия v${item.approved_version_number}` : "Утверждённая версия не зафиксирована"} · {item.campaign_role}</p><p className="muted">Используется в {item.plan_item_count} пунктах медиаплана · постов создано: {item.social_post_count}{postsToCreate ? ` · не создано: ${postsToCreate}` : ""}</p><div className="actions"><Link href={`/content/${item.id}`}>Открыть</Link><Link href={`/content/${item.id}#edit`}>Редактировать</Link><Link href={`/content/${item.id}#revision`}>Отправить на доработку</Link></div><p className="muted">Задача-источник: {item.source_task_title ? <Link href={`/tasks/${item.source_task_id}`}>{item.source_task_title}</Link> : "Связь не зафиксирована"} · публикаций по этой статье: {item.scheduled_publication_count} запланировано, {item.published_count} опубликовано</p></article>; })}</div> : <p className="empty-state">Статей пока нет.</p>}
        </section>
        <section id="publications" aria-labelledby="social-heading"><h3 id="social-heading">Посты для соцсетей <span className="count">{socialPosts.length}</span></h3>
          {socialPosts.length ? <div className="content-card-stack">{socialPosts.map(item => <article className="content-card" key={item.id}><h4><Link href={`/content/${item.id}`}>{item.title}</Link></h4><StatusBadge label={CONTENT_STATUS_LABELS[item.status]} status={item.status} /><p>{item.channel ?? "Канал не задан"}</p><div className="actions"><Link href={`/content/${item.id}`}>Открыть пост</Link><Link href={`/content/${item.id}#edit`}>Редактировать пост</Link><Link href={`/content/${item.id}#revision`}>Отправить пост на доработку</Link><Link href={`/campaigns/${id}/publications`}>Проверить публикации</Link></div></article>)}</div> : <p className="empty-state">Посты ещё не созданы. После утверждения пакета они появятся здесь.</p>}
          {socialPacks.length > 0 && <details className="disclosure"><summary>Пакеты постов ({socialPacks.length})</summary><ul className="simple-list">{socialPacks.map((pack) => <li key={pack.id}><Link href={`/content/${pack.id}`}>{pack.title}</Link><span>{CONTENT_STATUS_LABELS[pack.status]}</span><small>Постов: {socialPosts.filter((post) => post.parent_content_item_id === pack.id).length}</small></li>)}</ul></details>}
        </section>
      </div>
      <p className="muted">Ожидают согласования: {contents.filter((item) => ["ARTICLE", "SOCIAL_POST", "SOCIAL_POST_PACK"].includes(item.content_type) && item.status === "WAITING_APPROVAL").length}</p>
    </section>
    </>}
    {section === "plan" && <>
    <section className="page-section" id="publication-plan" aria-labelledby="plan-heading">
      <div className="section-heading"><div><p className="eyebrow">Планирование</p><h2 id="plan-heading">Медиаплан</h2><p className="muted">AI предложит расписание на основе утверждённых статей. План потребуется отдельно проверить и утвердить.</p></div>
        <div className="section-actions"><Link className="button-link secondary" href={`/campaigns/${id}/publications`}>К календарю</Link>{!archived && <button disabled={archived || planGenerationState === "queued"} onClick={generatePublicationPlan}>{planGenerationState === "queued" ? "Создаём медиаплан…" : "Создать медиаплан"}</button>}</div>
      </div>
      {changeState?.plan_requires_review && <div className="warning"><strong>Медиаплан создан по предыдущей версии стратегии. Требуется проверка.</strong><p>Существующий утверждённый план сохранён. Создание новой редакции выполняется отдельным действием.</p>{!changeState.has_pending_strategic_changes && !archived && <button disabled={archived} onClick={() => void generatePublicationPlan()}>Пересмотреть медиаплан</button>}</div>}
      {planGenerationState === "queued" && <p role="status" className="notice">Создаём медиаплан… {planGenerationRun ? `Состояние задачи: ${planGenerationRun.status === "QUEUED" ? "в очереди" : "выполняется"}.` : "Задача передаётся исполнителю."}{planGenerationRun && <Link href={`/tasks/${planGenerationRun.task_id}`}>Открыть задачу</Link>}</p>}
      {planGenerationState === "complete" && <p role="status" className="notice">Медиаплан создан{currentPlan ? `: ${currentPlan.items.filter((item) => item.status === "PLANNED").length} пунктов · статус: ${PLAN_STATUS_LABELS[currentPlan.status]}` : ""}. <a href={`/campaigns/${id}/plan`}>Проверить медиаплан</a></p>}
      {currentPlan ? renderPlanCard(currentPlan) : <p className="empty-state">План публикаций ещё не создан.</p>}
      {otherPlans.length > 0 && <details className="disclosure">
        <summary>Другие планы ({otherPlans.length})</summary>
        <div className="disclosure-body">{otherPlans.map(renderPlanCard)}</div>
      </details>}
      {publicationPlans.some((plan) => plan.items.some((item) => item.near_publication_warnings?.length)) && <p className="warning">В это время рядом запланирована другая публикация в том же канале.</p>}
    </section>
    </>}
    {section === "tasks" && <>
    <section className="page-section" aria-labelledby="tasks-heading">
      <div className="section-heading"><div><p className="eyebrow">Рабочий процесс</p><h2 id="tasks-heading">Задачи</h2><p className="muted">Сначала ошибки и блокировки, затем готовая и текущая работа.</p></div>{!archived && <Link className="button-link" href={`/tasks/new?campaign_id=${id}`}>Создать задачу</Link>}</div>
      <div className="summary-grid metric-cards">
        <Link className="metric-card" href={`/tasks?campaign_id=${id}&status=FAILED`}><strong>{tasks.filter((item) => item.status === "FAILED" && item.classification === "actionable").length}</strong><span>Ошибки</span><small>Нужен разбор</small></Link>
        <Link className="metric-card" href={`/tasks?campaign_id=${id}&status=BLOCKED`}><strong>{tasks.filter((item) => item.status === "BLOCKED").length}</strong><span>Заблокированы</span><small>Проверьте зависимости</small></Link>
        <Link className="metric-card" href={`/tasks?campaign_id=${id}&status=READY`}><strong>{tasks.filter((item) => item.status === "READY").length}</strong><span>Готовы к запуску</span><small>Следующий шаг</small></Link>
        <Link className="metric-card" href={`/tasks?campaign_id=${id}&status=IN_PROGRESS`}><strong>{tasks.filter((item) => item.status === "IN_PROGRESS").length}</strong><span>В работе</span><small>Выполняются сейчас</small></Link>
      </div>
      {displayAttentionTasks.filter(task => ["FAILED", "BLOCKED"].includes(task.status)).length ? <ul className="task-queue">{displayAttentionTasks.filter(task => ["FAILED", "BLOCKED"].includes(task.status)).map((task) => <li key={task.id}><Link href={`/tasks/${task.id}`}>{task.display_title}</Link><span>{TASK_TYPE_LABELS[task.task_type]} · {TASK_STATUS_LABELS[task.status]}</span>{task.error_summary && <div className="task-error-help"><p>{task.error_summary}</p>{task.next_action && <p>{task.next_action}</p>}{task.error_code && <details><summary>Технические сведения</summary><p>Код: <code>{task.error_code}</code></p></details>}</div>}</li>)}</ul> : <p className="empty-state">Нет задач, требующих внимания.</p>}
      {[["В работе", ["IN_PROGRESS"]], ["Ожидают решения или запуска", ["READY", "WAITING_REVIEW", "WAITING_APPROVAL", "NEW"]]].map(([label, statuses]) => <section key={String(label)}><h3>{String(label)}</h3><ul className="simple-list">{tasks.filter(task => (statuses as string[]).includes(task.status) && task.classification !== "superseded").map(task => <li key={task.id}><Link href={`/tasks/${task.id}`}>{task.title}</Link><span>{TASK_STATUS_LABELS[task.status]}</span></li>)}</ul></section>)}
      <section className="workflow-stages" aria-label="Основные этапы кампании"><h3>Основные этапы</h3><ol>{(["KNOWLEDGE_RESEARCH", "WRITE_ARTICLE", "CREATE_SOCIAL_POSTS"] as const).map((type) => { const task = tasks.find((item) => item.task_type === type); return <li key={type}><span>{TASK_TYPE_LABELS[type]}</span>{task ? <><Link href={`/tasks/${task.id}`}>{task.title}</Link><StatusBadge label={TASK_STATUS_LABELS[task.status]} status={task.status} /></> : <span className="muted">Ещё не создано</span>}</li>; })}</ol></section>
      <details className="disclosure"><summary>Завершённые и отменённые ({tasks.filter((task) => (["COMPLETED", "CANCELLED", "APPROVED"].includes(task.status) || task.classification === "historical" || task.classification === "superseded")).length})</summary><ul className="simple-list">{tasks.filter((task) => (["COMPLETED", "CANCELLED", "APPROVED"].includes(task.status) || task.classification === "historical" || task.classification === "superseded")).map((task) => <li key={task.id}><Link href={`/tasks/${task.id}`}>{task.title}</Link><span>{TASK_STATUS_LABELS[task.status]}</span></li>)}</ul></details>
    </section>
    </>}
    {section === "activity" && <>
    <section className="page-section" aria-labelledby="activity-heading">
      <div className="section-heading"><div><p className="eyebrow">Недавнее</p><h2 id="activity-heading">Активность</h2></div></div>
      {activities.length ? <ol className="activity-list">{activities.map((event) => <li key={event.id}><time dateTime={event.created_at}>{formatDateTime(event.created_at)}</time><span>{activityLabel(event)}{typeof event.metadata?.analysis_id === "string" && analyses.some(analysis => analysis.id === event.metadata?.analysis_id) && <> · <Link href={`/campaigns/${id}/performance#analysis-${event.metadata?.analysis_id}`}>Анализ результатов</Link></>}{event.task_id && tasks.some(task => task.id === event.task_id) && <> · <Link href={`/tasks/${event.task_id}`}>Задача</Link></>}{event.content_item_id && contents.some(content => content.id === event.content_item_id) && <> · <Link href={`/content/${event.content_item_id}`}>Материал</Link></>}{typeof event.metadata?.publication_id === "string" && publications.some(publication => publication.id === event.metadata?.publication_id) && <> · <Link href={`/campaigns/${id}/publications#publication-${event.metadata?.publication_id}`}>Публикация</Link></>}{typeof event.metadata?.publication_plan_id === "string" && publicationPlans.some(plan => plan.id === event.metadata?.publication_plan_id) && <> · <Link href={`/campaigns/${id}/plan#publication-plan-${event.metadata?.publication_plan_id}`}>Медиаплан</Link></>}</span></li>)}</ol> : <p className="empty-state">Событий пока нет.</p>}
    </section>
    </>}
    {section === "strategy" && <>
    {changeState?.has_pending_strategic_changes && <section className="page-section change-alert" id="campaign-change" aria-labelledby="campaign-change-heading">
      <p className="eyebrow">Изменения кампании · высокий приоритет</p><h2 id="campaign-change-heading">Маркетинговые вводные изменились</h2>
      <p>Изменения внесены после утверждения стратегии v{changeState.baseline_strategy_version}. Текущая стратегия остаётся source of truth, пока новая версия не пройдёт согласование.</p>
      <p className="warning">Медиаплан и существующие материалы созданы по предыдущей версии стратегии. Они сохранены без автоматического изменения.</p>
      <div className="actions"><button disabled={archived} onClick={() => void prepareStrategyRevision()}>Подготовить новую версию стратегии</button><Link className="button-link secondary-link" href={`/campaigns/${id}/edit`}>Просмотреть изменения</Link></div>
    </section>}
    <section id="strategy"><h2>Стратегия кампании и согласование · активная версия {campaign.strategy_version}</h2>
      <div className="page-section strategy">
        {campaign.status === "DRAFT" && <><p>Стратегия ещё не сформирована.</p><button disabled={archived} onClick={generate}>Сформировать стратегию</button></>}
        {campaign.status === "PLANNING" && <p>Идёт подготовка стратегии.{tasks.find((item) => item.task_type === "CAMPAIGN_PLANNING") && ` Статус задачи: ${TASK_STATUS_LABELS[tasks.find((item) => item.task_type === "CAMPAIGN_PLANNING")!.status]}.`}</p>}
        {pendingStrategyApproval && pendingStrategyApproval.subject_version > campaign.strategy_version && <p className="notice">Новая стратегия v{pendingStrategyApproval.subject_version} ожидает согласования. До решения продолжает действовать утверждённая v{campaign.strategy_version}.</p>}
        {strategyToReview && <><h3>Резюме</h3><p>{strategyToReview.campaign_summary}</p><h3>Позиционирование</h3><p>{strategyToReview.positioning}</p><h3>Целевая аудитория</h3><p>{strategyToReview.target_audience}</p><h3>Главное сообщение</h3><p>{strategyToReview.main_message}</p><h3>Контент-стратегия</h3><p>{strategyToReview.content_strategy}</p><h3>Темы контента</h3><ul>{(strategyToReview.content_topics ?? []).map((topic) => <li key={topic}>{topic}</li>)}</ul><h3>Рекомендуемая статья</h3><p><strong>{strategyToReview.recommended_article?.title}</strong> — {strategyToReview.recommended_article?.objective}; {strategyToReview.recommended_article?.angle}; CTA: {strategyToReview.recommended_article?.cta}</p><h3>Подход к соцсетям</h3><p>{strategyToReview.social_strategy?.approach} ({strategyToReview.social_strategy?.post_count} публикаций: {strategyToReview.social_strategy?.channels?.join(", ")})</p><h3>Рекомендуемые задачи</h3><ol>{(strategyToReview.tasks ?? []).map((item) => <li key={item.key}><strong>{item.title}</strong> — {item.agent_slug}{item.depends_on?.length ? `; зависит от: ${item.depends_on.join(", ")}` : ""}</li>)}</ol></>}
        {campaign.status === "ACTIVE" && <p><strong>Стратегия утверждена.</strong> Версия {campaign.strategy_version}.</p>}
        {campaign.status === "WAITING_APPROVAL" && <div className="actions"><button disabled={archived} onClick={approve}>Утвердить стратегию</button><button disabled={archived} className="secondary" onClick={() => setAction("revision")}>Запросить доработку</button><button disabled={archived} className="secondary" onClick={() => setAction("reject")}>Отклонить</button></div>}
        {action && <div><label htmlFor="strategy-comment">{action === "revision" ? "Что необходимо изменить?" : "Причина отклонения"}</label><textarea id="strategy-comment" value={comment} onChange={(event) => setComment(event.target.value)} /><div className="actions"><button disabled={archived} onClick={submitDecision}>Отправить</button><button disabled={archived} className="secondary" onClick={() => setAction(null)}>Отмена</button></div></div>}
        <h3>История согласования</h3>{approvals.length ? <ul>{approvals.map((approval) => <li key={approval.id}>Версия {approval.subject_version}: {approval.status}{approval.resolved_at ? ` — ${formatDateTime(approval.resolved_at)}` : ""}{approval.comment && <p>Комментарий: {approval.comment}</p>}</li>)}</ul> : <p>Согласований пока нет.</p>}
        <dl className="campaign-facts"><div><dt>Дополнительное описание</dt><dd>{campaign.description ?? "Нет дополнительного контекста"}</dd></div><div><dt>Создал</dt><dd>{campaign.creator.full_name ?? campaign.creator.email}</dd></div><div><dt>Создана</dt><dd>{formatDateTime(campaign.created_at)}</dd></div><div><dt>Обновлена</dt><dd>{formatDateTime(campaign.updated_at)}</dd></div></dl>
      </div>
    </section>
    </>}
    {section === "publications" && <>
    <div className="content-columns">
      <section className="page-section" aria-labelledby="schedule-heading">
        <div className="section-heading"><div><p className="eyebrow">Расписание</p><h2 id="schedule-heading">Предстоящие публикации</h2></div><Link className="button-link secondary" href={`/campaigns/${id}/publications`}>Открыть календарь</Link></div>
        {upcomingPublications.length ? <ol className="simple-list" aria-label="Предстоящие публикации">{upcomingPublications.map((item) => <li key={item.id}><Link href={`/content/${item.content_item_id}`}>{contents.find((content) => content.id === item.content_item_id)?.title ?? "Материал"}</Link><span>{item.channel} · {formatDateTime(item.scheduled_at ?? "")}</span><small>{PUBLICATION_STATUS_LABELS[item.status]}</small></li>)}</ol> : calendarUpcoming.length ? <ol className="simple-list" aria-label="Предстоящие публикации">{calendarUpcoming.map((item) => <li key={item.publication_id}><Link href={`/content/${item.content_item_id}`}>{item.title}</Link><span>{item.channel} · {formatDateTime(item.scheduled_at ?? "")}</span><small>{PUBLICATION_STATUS_LABELS[item.status]}</small></li>)}</ol> : [...publications, ...calendarItems].some((item) => item.is_overdue) ? <p role="alert">Есть просроченные публикации. <Link href="/publications#overdue">Принять решение</Link></p> : <p className="empty-state">Запланированных публикаций нет.</p>}
      </section>
      <section className="page-section" aria-labelledby="published-heading">
        <div className="section-heading"><div><p className="eyebrow">История</p><h2 id="published-heading">Уже опубликовано</h2></div></div>
        {publishedItems.length ? <ul className="simple-list">{publishedItems.slice(0, 8).map((item) => <li key={item.id}><Link href={`/content/${item.content_item_id}`}>{contents.find((content) => content.id === item.content_item_id)?.title ?? "Материал"}</Link><span>{item.channel}{item.published_at || item.updated_at ? ` · ${formatDateTime(item.published_at ?? item.updated_at)}` : " · дата не указана"}</span><StatusBadge label={PUBLICATION_STATUS_LABELS[item.status]} tone="success" /></li>)}</ul> : <p className="empty-state">Опубликованных материалов пока нет.</p>}
        {calendarGroups && <details className="disclosure"><summary>Календарные записи ({Object.values(calendarGroups).flat().length})</summary>{Object.entries(calendarGroups).map(([date, items]) => <section key={date}><h3>{date}</h3><ul className="simple-list">{items.map((item) => <li key={item.publication_id}><Link href={`/content/${item.content_item_id}`}>{item.title}</Link><span>{item.channel} · {PUBLICATION_STATUS_LABELS[item.status]} · {item.scheduled_at ? formatDateTime(item.scheduled_at) : item.published_at ? formatDateTime(item.published_at) : "время не указано"}</span></li>)}</ul></section>)}</details>}
      </section>
    </div>
<section className="page-section"><h2>Публикации кампании</h2>{publications.map(publication => <article className="content-card" id={`publication-${publication.id}`} key={publication.id}><h3>{contents.find(c => c.id === publication.content_item_id)?.title ?? "Материал"} · {publication.channel}</h3><p>{PUBLICATION_STATUS_LABELS[publication.status]} · {formatDateTime(publication.published_at ?? publication.scheduled_at ?? publication.created_at)}</p><p className="muted">Точная версия: {publication.content_version_id}</p><Link href={`/content/${publication.content_item_id}#publication`}>Открыть доставку и версию</Link></article>)}</section>{socialPosts.map(renderPublication)}</>}
    {section === "performance" && <div className="performance-workspace"><h2>Результаты и оптимизация</h2><nav className="local-navigation" aria-label="Разделы результатов">{[["goals","Цели кампании"],["quality","Качество данных"],["results","Результаты публикаций"],["feedback","Обратная связь"],["analyses","Анализы"],["recommendations","Рекомендации к изменениям"],["experiments","Эксперименты"],["provenance","Почему предложено изменение"]].map(([anchor,label]) => <Link key={anchor} href={`#${anchor}`}>{label}</Link>)}</nav>
<section id="goals" className="page-section">        <CampaignKPIBlock campaignId={id} archived={campaign?.status === "ARCHIVED"} performance={performance} onChange={load} /></section>
<section id="quality" className="page-section"><h2>Качество данных</h2>{performance ? <><p>Опубликовано: {performance.total_published} · С метриками: {performance.with_metrics} · Без метрик: {performance.total_published - performance.with_metrics} · Отзывов: {feedback.length}</p>{Object.entries(performance.metric_coverage).map(([metric,ratio]) => <span key={metric}>{metric}: {Math.round(ratio * 100)}% · </span>)}<p>Последнее наблюдение: {performance.publications.map(p => p.observed_at ?? p.metrics?.observed_at).filter((x): x is string => Boolean(x)).sort().at(-1) ?? "Нет данных"}</p><p>Ноль — измеренное значение. «Нет данных» означает отсутствие наблюдения.</p></> : <p>Нет данных</p>}</section>
<section id="results" className="page-section"><h2>Результаты публикаций</h2>        {performance && <PublicationPerformanceTable performance={performance} />}        {performance ? <><div className="summary-grid metric-cards"><div className="metric-card"><strong>{performance.total_published}</strong><span>Опубликовано</span></div><div className="metric-card"><strong>{performance.with_metrics}</strong><span>С метриками</span></div><div className="metric-card"><strong>{performance.totals.views ?? "Нет данных"}</strong><span>Просмотры</span></div><div className="metric-card"><strong>{performance.totals.likes ?? "Нет данных"}</strong><span>Лайки</span></div></div><p>Ноль — измеренное значение. «Нет данных» означает, что показатель не передан или ещё не синхронизирован.</p><ul>{performance.publications.map((row) => { const publication = publications.find((item) => item.id === row.publication_id); const latest = row.metrics; return <li key={row.publication_id}><Link href={`/content/${row.content_item_id}`}>{row.content_title ?? "Материал"}</Link> · просмотры: {latest?.views ?? "Нет данных"}{latest ? ` · источник: ${latest.source === "MANUAL" ? "вручную" : latest.provider ?? "провайдер"}, ${formatDateTime(latest.observed_at)}` : " · не синхронизировано"}{publication?.status === "PUBLISHED" && <button className="secondary" disabled={archived || metricsBusy === row.publication_id} onClick={() => addManualMetrics(row.publication_id)}>Внести метрики вручную</button>}</li>; })}</ul></> : <p>Метрики пока недоступны.</p>}</section>
<section id="feedback" className="page-section"><h2>Обратная связь</h2><button disabled={archived} onClick={addFeedback}>Добавить обратную связь</button>        {feedback.length ? <ul>{feedback.map((item) => <li key={item.id}>{item.category}: {item.comment} · {formatDateTime(item.created_at)}</li>)}</ul> : <p>Обратной связи пока нет.</p>}</section>
<section id="analyses" className="page-section"><h2>Анализы</h2><button disabled={archived} className="secondary" onClick={generateFeedbackAnalysis}>Сформировать выводы</button>        {analyses.length ? analyses.map((analysis) => <article className="card" id={`analysis-${analysis.id}`} key={analysis.id}><p><strong>Выводы: {analysis.status}</strong>{analysis.generated_at ? ` · ${formatDateTime(analysis.generated_at)}` : ""}</p>{workspace?.superseded_legacy_analysis_ids?.includes(analysis.id) && <p>Исторический черновик — есть более новый рассмотренный анализ.</p>}<p>{analysis.summary}</p><PerformanceAnalysisDetails analysis={analysis} archived={campaign.status === "ARCHIVED"} retry={() => { void feedbackApi.retry(analysis.id).then(loadFeedback).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Не удалось повторить анализ.")); }} />{analysis.limitations.map((item) => <p key={item}>Ограничение: {item}</p>)}{analysis.findings.map((item, index) => <p key={index}>Наблюдение: {String(item.observation ?? "")}</p>)}{analysis.status === "DRAFT" && analysis.generated_at && !workspace?.superseded_legacy_analysis_ids?.includes(analysis.id) && <div className="actions"><button disabled={archived} onClick={() => reviewFeedbackAnalysis(analysis.id, "accept")}>Принять как рекомендации</button><button disabled={archived} className="secondary" onClick={() => reviewFeedbackAnalysis(analysis.id, "reject")}>Отклонить выводы</button></div>}</article>) : <p>Выводы ещё не сформированы.</p>}</section>
<section id="recommendations" className="page-section"><h2>Рекомендации к изменениям</h2>{analyses.filter(a => a.status === "ACCEPTED").map(analysis => <OptimizationProposalPanel key={analysis.id} analysis={analysis} archived={archived} />)}</section>
<section id="experiments" className="page-section"><h2>Эксперименты</h2>        <MarketingExperimentsPanel campaignId={campaign.id} archived={campaign.status === "ARCHIVED"} /></section>
<section id="provenance" className="page-section"><h2>Почему предложено изменение</h2><p>Выберите «Почему предложено?» в карточке рекомендации. Цепочка строится только по сохранённым связям.</p></section>
</div>}
    </div>
  </div>;
}
