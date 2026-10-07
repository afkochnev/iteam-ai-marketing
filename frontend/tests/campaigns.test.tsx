import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CampaignDetailsPage from "../app/campaigns/[id]/page";
import EditCampaignPage from "../app/campaigns/[id]/edit/page";
import NewCampaignPage from "../app/campaigns/new/page";
import CampaignsPage from "../app/campaigns/page";

const { replace, push, list, get, workspace, agentsList, agentRunList, agentRunGet, create, taskCreate, taskGet, taskRun, update, previewChange, applyChange, prepareStrategyRevision, archive, generateStrategy, approveStrategy, requestRevision, rejectStrategy, taskList, approvalList, contentList, contentGet, activityList, publicationList, publicationCalendar, publicationCreate, publicationScheduleContent, publicationApprove, publicationSchedule, publicationCancel, publicationPublishNow, publicationRetry, publicationReconcilePublished, publicationReconcileNotPublished, feedbackList, feedbackAnalyses, feedbackCreate, feedbackGenerate, feedbackAccept, feedbackReject, performance, plansList, planGenerate, planTransition, planRevise, planAddItem, planUpdateItem, planRemoveItem, planReorder, manualMetrics } = vi.hoisted(() => ({
  replace: vi.fn(), push: vi.fn(), list: vi.fn(), get: vi.fn(), workspace: vi.fn(), agentsList: vi.fn(), agentRunList: vi.fn(), agentRunGet: vi.fn(), create: vi.fn(), taskCreate: vi.fn(), taskGet: vi.fn(), taskRun: vi.fn(), update: vi.fn(), previewChange: vi.fn(), applyChange: vi.fn(), prepareStrategyRevision: vi.fn(), archive: vi.fn(), generateStrategy: vi.fn(), approveStrategy: vi.fn(), requestRevision: vi.fn(), rejectStrategy: vi.fn(), taskList: vi.fn(), approvalList: vi.fn(), contentList: vi.fn(), contentGet: vi.fn(), activityList: vi.fn(), publicationList: vi.fn(), publicationCalendar: vi.fn(), publicationCreate: vi.fn(), publicationScheduleContent: vi.fn(), publicationApprove: vi.fn(), publicationSchedule: vi.fn(), publicationCancel: vi.fn(), publicationPublishNow: vi.fn(), publicationRetry: vi.fn(), publicationReconcilePublished: vi.fn(), publicationReconcileNotPublished: vi.fn(), feedbackList: vi.fn(), feedbackAnalyses: vi.fn(), feedbackCreate: vi.fn(), feedbackGenerate: vi.fn(), feedbackAccept: vi.fn(), feedbackReject: vi.fn(), performance: vi.fn(), plansList: vi.fn(), planGenerate: vi.fn(), planTransition: vi.fn(), planRevise: vi.fn(), planAddItem: vi.fn(), planUpdateItem: vi.fn(), planRemoveItem: vi.fn(), planReorder: vi.fn(), manualMetrics: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push }), useParams: () => ({ id: "campaign-1" }) }));
vi.mock("@/components/auth-provider", () => ({ useAuth: () => ({ user: { role: "ADMIN" }, loading: false }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, campaignsApi: { list, get, workspace, create, update, previewChange, applyChange, prepareStrategyRevision, archive, generateStrategy, approveStrategy, requestStrategyRevision: requestRevision, rejectStrategy }, agentsApi: { list: agentsList }, agentRunsApi: { ...actual.agentRunsApi, list: agentRunList, get: agentRunGet }, approvalsApi: { list: approvalList, get: vi.fn() }, tasksApi: { ...actual.tasksApi, list: taskList, get: taskGet, create: taskCreate, run: taskRun }, contentApi: { ...actual.contentApi, list: contentList, get: contentGet }, activitiesApi: { list: activityList }, feedbackApi: { list: feedbackList, analyses: feedbackAnalyses, create: feedbackCreate, generate: feedbackGenerate, accept: feedbackAccept, reject: feedbackReject }, metricsApi: { campaign: performance }, publicationPlansApi: { list: plansList, generate: planGenerate, submit: planTransition, approve: planTransition, reject: planTransition, revise: planRevise, addItem: planAddItem, updateItem: planUpdateItem, removeItem: planRemoveItem, reorder: planReorder }, publicationsApi: { listCampaign: publicationList, calendar: publicationCalendar, approve: publicationApprove, create: publicationCreate, scheduleContent: publicationScheduleContent, schedule: publicationSchedule, cancel: publicationCancel, publishNow: publicationPublishNow, retry: publicationRetry, reconcilePublished: publicationReconcilePublished, reconcileNotPublished: publicationReconcileNotPublished, addManualMetrics: manualMetrics } };
});

const campaign = {
  id: "campaign-1", name: "AI-диагностика", description: "Контекст", goal: "30 заявок",
  product: "Диагностика", target_audience: "CEO", offer: "Онлайн-диагностика",
  desired_result: "30 заявок", start_date: "2026-10-01", end_date: "2026-10-31",
  status: "DRAFT" as const, strategy: null, strategy_version: 0, created_by: "user-1",
  creator: { id: "user-1", full_name: "Admin", email: "admin@example.com" },
  created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z",
};

describe("Campaigns UI", () => {
  it("opens read-only Director chat from Campaign", async () => {
    render(<CampaignDetailsPage />);
    expect(await screen.findByRole("link", { name: "Обсудить с Директором по маркетингу" })).toHaveAttribute("href", "/campaigns/campaign-1/director");
  });
  afterEach(() => cleanup());
  beforeEach(() => { vi.clearAllMocks(); list.mockResolvedValue([campaign]); get.mockResolvedValue(campaign); workspace.mockResolvedValue({ campaign, director: { strategy_status: "ACTIVE", article_count: 0, approved_article_count: 0, plan_status: null, plan_item_count: 0, plan_items_with_posts: 0, plan_items_without_posts: 0, social_post_count: 0, posts_waiting_approval: 0, scheduled_publication_count: 0, published_count: 0, failed_task_count: 0, blocked_task_count: 0, next_step: { title: "Проверить материалы", description: "Следующее действие", href: "/content", entity_type: "article", entity_id: null } }, knowledge: { store_ready: true, source_count: 0, item_count: 0, ready_item_count: 0, processing_item_count: 0, failed_item_count: 0, campaign_packs: [], has_current_strategy_pack: false }, articles: [], social_posts: [], publication_plan: null, other_plans: [], publications: [], attention_tasks: [], feedback: { new_feedback_count: 0, new_metrics_count: 0 } }); agentsList.mockResolvedValue([]); agentRunGet.mockResolvedValue({ id: "run-1", task_id: "task-1", agent_id: "agent-1", campaign_id: campaign.id, status: "COMPLETED", model: "test", created_at: campaign.created_at }); agentRunList.mockResolvedValue([{ id: "run-created", status: "COMPLETED", created_at: campaign.created_at }]); taskCreate.mockResolvedValue({ id: "task-created", status: "READY" }); taskGet.mockResolvedValue({ id: "task-created", status: "COMPLETED", output_data: { content_item_id: "post-created" } }); taskRun.mockResolvedValue({ id: "run-created", status: "QUEUED" }); taskList.mockResolvedValue([]); approvalList.mockResolvedValue([]); contentList.mockResolvedValue([]); activityList.mockResolvedValue([]); publicationList.mockResolvedValue([]); publicationCalendar.mockResolvedValue([]); feedbackList.mockResolvedValue([]); feedbackAnalyses.mockResolvedValue([]); performance.mockResolvedValue(null); plansList.mockResolvedValue([]); vi.spyOn(window, "confirm").mockReturnValue(true); });

  it("offers ARTICLE actions with shared card spacing", async () => {
    const value = await workspace();
    workspace.mockResolvedValue({ ...value, articles: [{ id: "article-24", title: "Article 24", status: "APPROVED", campaign_role: "Источник", plan_item_count: 0, social_post_count: 0, scheduled_publication_count: 0, published_count: 0 }] });
    const { container } = render(<CampaignDetailsPage />);
    expect(await screen.findByRole("link", { name: "Редактировать" })).toHaveAttribute("href", "/content/article-24#edit");
    expect(screen.getByRole("link", { name: "Отправить на доработку" })).toHaveAttribute("href", "/content/article-24#revision");
    expect(screen.getByRole("link", { name: "Открыть" })).toHaveAttribute("href", "/content/article-24");
    expect(container.querySelector(".article-assets.content-card-stack .article-asset")).toBeInTheDocument();
  });

  it("renders campaign list and empty state", async () => {
    const first = render(<CampaignsPage />);
    expect(await screen.findByText("AI-диагностика")).toBeInTheDocument();
    first.unmount(); list.mockResolvedValue([]); render(<CampaignsPage />);
    expect(await screen.findByText("Кампаний пока нет")).toBeInTheDocument();
  });

  it("shows campaign list API error", async () => {
    list.mockRejectedValue(new Error("Ошибка загрузки")); render(<CampaignsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка загрузки");
  });

  it("validates required fields and redirects after creation", async () => {
    create.mockResolvedValue(campaign); render(<NewCampaignPage />);
    fireEvent.click(screen.getByRole("button", { name: "Создать кампанию" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Название обязательно.");
    fireEvent.change(screen.getByLabelText("Название *"), { target: { value: "AI-диагностика" } });
    fireEvent.change(screen.getByLabelText("Цель *"), { target: { value: "30 заявок" } });
    fireEvent.click(screen.getByRole("button", { name: "Создать кампанию" }));
    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(push).toHaveBeenCalledWith("/campaigns/campaign-1");
  });

  it("renders details and archives after confirmation", async () => {
    archive.mockResolvedValue({ ...campaign, status: "ARCHIVED" }); render(<CampaignDetailsPage />);
    expect(await screen.findByRole("heading", { name: "AI-диагностика" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Архивировать" }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith("campaign-1"));
    expect(screen.queryByText("Внести изменения")).not.toBeInTheDocument();
  });

  it("shows a director brief, article provenance, and the five-step plan pipeline", async () => {
    const article = { id: "article-1", title: "Связанная статья", status: "APPROVED", content_type: "ARTICLE", current_version_id: "version-2", current_version_number: 2, approved_version_id: "version-2", approved_version_number: 2, source_task_id: "write-task", source_task_title: "Написать статью", source_task_status: "COMPLETED", parent_content_item_id: null, publication_plan_item_id: null, channel: null, campaign_role: "Рекомендована стратегией", plan_item_count: 1, social_post_count: 0, scheduled_publication_count: 0, published_count: 0 } as const;
    const planItem = { id: "plan-item-1", position: 1, scheduled_at: "2026-10-06T12:00:00Z", channel: "VK", topic: "Тема медиаплана", purpose: "Помочь читателю", format: "post", message_brief: "Краткий бриф", source_content_item_id: article.id, source_content_item_title: article.title, source_content_version_id: "version-2", source_version_number: 2, source_claim_ids: ["claim-1"], source_support_summary: "Есть подтверждение в источнике", status: "PLANNED", social_posts: [], publications: [], pipeline: [{ label: "Статья", status: "available", href: "/content/article-1", action_label: "v2" }, { label: "Пост", status: "not_created", href: "/campaigns/campaign-1#plan-item-plan-item-1", action_label: "Создать пост" }, { label: "Согласование", status: "not_started", href: null, action_label: null }, { label: "Запланировано", status: "not_scheduled", href: "/campaigns/campaign-1#plan-item-plan-item-1", action_label: "Не запланировано" }, { label: "Опубликовано", status: "not_published", href: null, action_label: "Ожидает доставки" }], post_action: { allowed: true, error_code: null, reason: null, next_action: null, existing_task_id: null } } as const;
    const plan = { id: "plan-1", campaign_id: campaign.id, status: "APPROVED", planning_horizon_start: "2026-10-01T00:00:00Z", planning_horizon_end: "2026-10-15T00:00:00Z", timezone_policy: "Europe/Bratislava", created_by_user_id: "user-1", generated_by_agent_run_id: null, feedback_analysis_id: null, approved_at: null, approved_by_user_id: null, items: [{ ...planItem, angle: "Практический угол", near_publication_warnings: [] }] } as const;
    workspace.mockResolvedValue({ campaign, director: { strategy_status: "APPROVED", article_count: 1, approved_article_count: 1, plan_status: "APPROVED", plan_item_count: 1, plan_items_with_posts: 0, plan_items_without_posts: 1, social_post_count: 0, posts_waiting_approval: 0, scheduled_publication_count: 0, published_count: 0, failed_task_count: 0, blocked_task_count: 0, next_step: { title: "Создать VK-пост", description: "Для пункта медиаплана ещё нет поста", href: "/campaigns/campaign-1#plan-item-plan-item-1", entity_type: "publication_plan_item", entity_id: planItem.id } }, knowledge: { store_ready: true, source_count: 1, item_count: 2, ready_item_count: 2, processing_item_count: 0, failed_item_count: 0, campaign_packs: [], has_current_strategy_pack: true }, articles: [article], social_posts: [], publication_plan: { ...plan, item_count: 1, items: [planItem] }, other_plans: [], publications: [], attention_tasks: [], feedback: { new_feedback_count: 0, new_metrics_count: 0 } });
    plansList.mockResolvedValue([plan]); contentList.mockResolvedValue([{ id: article.id, campaign_id: campaign.id, content_type: "ARTICLE", title: article.title, status: "APPROVED", current_version_number: 2, created_at: campaign.created_at, updated_at: campaign.updated_at }]);
    agentsList.mockResolvedValue([{ id: "smm-1", name: "SMM Manager", slug: "smm_manager", role: "smm_manager", status: "ACTIVE", model: "test", autonomy_level: 2 }]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByRole("heading", { name: "Создать VK-пост" })).toBeInTheDocument();
    expect(screen.getByText(/Утверждена версия v2/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Написать статью" })).toHaveAttribute("href", "/tasks/write-task");
    expect(screen.getByText("Точная версия источника: v2 · подтверждений: 1")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Цепочка пункта 1" }).children).toHaveLength(5);
    expect(screen.getByRole("button", { name: "Создать пост" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть базу знаний и исследование" })).toHaveAttribute("href", "/knowledge?campaign_id=campaign-1");

    let resolveTask!: (value: { id: string }) => void;
    taskCreate.mockReturnValue(new Promise((resolve) => { resolveTask = resolve; }));
    fireEvent.click(screen.getByRole("button", { name: "Создать пост" }));
    expect(screen.getByRole("button", { name: "Создаём задачу…" })).toBeDisabled();
    resolveTask({ id: "task-created" });
    expect(await screen.findByText("Задача создана и ожидает автоматического запуска")).toBeInTheDocument();
    expect(taskRun).not.toHaveBeenCalled();
    const href = screen.getByRole("link", { name: "Перейти к следующему шагу" }).getAttribute("href")!;
    expect(document.getElementById(href.split("#")[1])).toBeInTheDocument();

    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 3_100)); });
    expect(await screen.findByRole("link", { name: "Проверить пост" })).toHaveAttribute("href", "/content/post-created");
    expect(workspace.mock.calls.length).toBeGreaterThan(1);
  });

  it("does not offer post creation when the exact source article version is unverified", async () => {
    const article = { id: "article-1", title: "Связанная статья", status: "APPROVED", content_type: "ARTICLE", current_version_id: null, current_version_number: null, approved_version_id: null, approved_version_number: null, source_task_id: null, source_task_title: null, source_task_status: null, parent_content_item_id: null, publication_plan_item_id: null, channel: null, campaign_role: "Источник плана", plan_item_count: 1, social_post_count: 0, scheduled_publication_count: 0, published_count: 0 } as const;
    const planItem = { id: "plan-item-1", position: 1, scheduled_at: "2026-10-06T12:00:00Z", channel: "VK", topic: "Тема медиаплана", purpose: "Помочь читателю", format: "post", message_brief: "Краткий бриф", source_content_item_id: article.id, source_content_item_title: article.title, source_content_version_id: null, source_version_number: null, source_claim_ids: null, source_support_summary: null, status: "PLANNED", social_posts: [], publications: [], pipeline: [{ label: "Статья", status: "unverified", href: "/content/article-1", action_label: "Версия источника не подтверждена" }, { label: "Пост", status: "not_created", href: "/campaigns/campaign-1#plan-item-plan-item-1", action_label: null }, { label: "Согласование", status: "not_started", href: null, action_label: null }, { label: "Запланировано", status: "not_scheduled", href: "/campaigns/campaign-1#plan-item-plan-item-1", action_label: null }, { label: "Опубликовано", status: "not_published", href: null, action_label: null }], post_action: { allowed: false, error_code: "PUBLICATION_PLAN_SOURCE_INVALID", reason: "Версия источника не подтверждена", next_action: "Проверьте источник", existing_task_id: null } } as const;
    const plan = { id: "plan-1", campaign_id: campaign.id, status: "APPROVED", planning_horizon_start: "2026-10-01T00:00:00Z", planning_horizon_end: "2026-10-15T00:00:00Z", timezone_policy: "Europe/Bratislava", created_by_user_id: "user-1", generated_by_agent_run_id: null, feedback_analysis_id: null, approved_at: null, approved_by_user_id: null, items: [{ ...planItem, angle: "Практический угол", near_publication_warnings: [] }] } as const;
    workspace.mockResolvedValue({ campaign, director: { strategy_status: "APPROVED", article_count: 1, approved_article_count: 1, plan_status: "APPROVED", plan_item_count: 1, plan_items_with_posts: 0, plan_items_without_posts: 1, social_post_count: 0, posts_waiting_approval: 0, scheduled_publication_count: 0, published_count: 0, failed_task_count: 0, blocked_task_count: 0, next_step: { title: "Проверить источник", description: "Версия источника не подтверждена", href: "/content/article-1", entity_type: "content_item", entity_id: article.id } }, knowledge: { store_ready: true, source_count: 0, item_count: 0, ready_item_count: 0, processing_item_count: 0, failed_item_count: 0, campaign_packs: [], has_current_strategy_pack: false }, articles: [article], social_posts: [], publication_plan: { ...plan, item_count: 1, items: [planItem] }, other_plans: [], publications: [], attention_tasks: [], feedback: { new_feedback_count: 0, new_metrics_count: 0 } });
    plansList.mockResolvedValue([plan]);
    contentList.mockResolvedValue([{ id: article.id, campaign_id: campaign.id, content_type: "ARTICLE", title: article.title, status: "APPROVED", current_version_number: null, created_at: campaign.created_at, updated_at: campaign.updated_at }]);

    render(<CampaignDetailsPage />);
    expect((await screen.findAllByText("Версия источника не подтверждена")).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Пост пока нельзя создать.")).toBeInTheDocument();
    expect(screen.getByText("Проверьте источник")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Создать пост" })).not.toBeInTheDocument();
  });

  it("shows loading and completion feedback for media plan creation", async () => {
    const generated = { id: "new-plan", campaign_id: campaign.id, status: "DRAFT" as const, planning_horizon_start: "2026-10-01T00:00:00Z", planning_horizon_end: "2026-10-14T00:00:00Z", timezone_policy: "UTC", created_by_user_id: "user-1", generated_by_agent_run_id: "run-1", feedback_analysis_id: null, approved_at: null, approved_by_user_id: null, items: [] as never[] };
    let resolvePlan!: (plan: typeof generated) => void;
    const pendingPlan = new Promise<typeof generated>((resolve) => { resolvePlan = resolve; });
    planGenerate.mockReturnValue(pendingPlan);
    plansList.mockResolvedValue([generated]);
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Создать медиаплан" }));
    expect(await screen.findByRole("button", { name: "Создаём медиаплан…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Создаём медиаплан…" })).toBeDisabled();
    resolvePlan(generated);
    await waitFor(() => expect(planGenerate).toHaveBeenCalledWith("campaign-1", expect.objectContaining({ total_items: 6 })));
    expect(await screen.findByText(/Медиаплан создан/)).toBeInTheDocument();
  });

  it("explains why media plan generation failed and links the next action", async () => {
    const { ApiError } = await import("../lib/api");
    planGenerate.mockRejectedValue(new ApiError("No approved articles", 409, "APPROVED_ARTICLES_NOT_FOUND"));
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Создать медиаплан" }));
    expect(await screen.findByText("В кампании пока нет утверждённых статей.")).toBeInTheDocument();
    expect(screen.getByText(/утвердите хотя бы одну статью/)).toBeInTheDocument();
    expect(screen.getByText("APPROVED_ARTICLES_NOT_FOUND")).toBeInTheDocument();
  });

  it("keeps archived campaign read-only", async () => {
    get.mockResolvedValue({ ...campaign, status: "ARCHIVED" }); render(<CampaignDetailsPage />);
    expect(await screen.findByText("Архив")).toBeInTheDocument();
    expect(screen.queryByText("Внести изменения")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Архивировать" })).not.toBeInTheDocument();
  });

  it("saves an administrative title change without a strategy revision", async () => {
    previewChange.mockResolvedValue({ kind: "ADMINISTRATIVE", changes: [{ field: "name", label: "Название", old_value: "AI-диагностика", new_value: "Новое название", kind: "ADMINISTRATIVE" }], impact: { strategy_version: 0, strategy_status: "DRAFT", publication_plan_id: null, publication_plan_status: null, publication_plan_item_count: 0, future_plan_item_count: 0, approved_social_post_count: 0, scheduled_publication_count: 0, published_publication_count: 0, scheduled_publications: [] }, requires_confirmation: false, warning: null, guarantees: ["Стратегия не изменится"] });
    applyChange.mockResolvedValue({ campaign: { ...campaign, name: "Новое название" } });
    render(<EditCampaignPage />);
    fireEvent.change(await screen.findByLabelText("Название *"), { target: { value: "Новое название" } });
    fireEvent.click(screen.getByRole("button", { name: "Проверить изменения" }));
    expect(await screen.findByRole("heading", { name: "Вы меняете" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Сохранить изменения" }));
    await waitFor(() => expect(applyChange).toHaveBeenCalledWith("campaign-1", expect.objectContaining({ confirmed_impact: false })));
    expect(push).toHaveBeenCalledWith("/campaigns/campaign-1");
  });

  it("shows strategic impact before saving target audience changes", async () => {
    previewChange.mockResolvedValue({ kind: "STRATEGIC", changes: [{ field: "target_audience", label: "Целевая аудитория", old_value: "CEO", new_value: "Собственники компаний", kind: "STRATEGIC" }], impact: { strategy_version: 1, strategy_status: "APPROVED", publication_plan_id: "plan-1", publication_plan_status: "APPROVED", publication_plan_item_count: 16, future_plan_item_count: 12, approved_social_post_count: 2, scheduled_publication_count: 1, published_publication_count: 3, scheduled_publications: [{ id: "publication-1", channel: "TELEGRAM", scheduled_at: "2026-10-10T10:00:00Z" }] }, requires_confirmation: true, warning: "Эти изменения могут повлиять на утверждённую стратегию и медиаплан.", guarantees: ["Текущая утверждённая стратегия не будет изменена автоматически.", "Запланированные Publications не будут изменены."] });
    applyChange.mockResolvedValue({ campaign: { ...campaign, target_audience: "Собственники компаний" } });
    get.mockResolvedValue({ ...campaign, status: "ACTIVE", strategy_version: 1 });
    render(<EditCampaignPage />);
    fireEvent.change(await screen.findByLabelText("Целевая аудитория"), { target: { value: "Собственники компаний" } });
    fireEvent.click(screen.getByRole("button", { name: "Проверить изменения" }));
    expect(await screen.findByText("Эти изменения могут повлиять на утверждённую стратегию и медиаплан.")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText(/Утверждён · 16 активных/)).toBeInTheDocument();
    expect(screen.getByText("TELEGRAM")).toBeInTheDocument();
    expect(screen.getByText("Текущая утверждённая стратегия не будет изменена автоматически.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Предложение / оффер"), { target: { value: "Новый оффер" } });
    expect(screen.queryByRole("button", { name: "Сохранить изменения" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Проверить изменения" }));
    expect(await screen.findByRole("button", { name: "Сохранить изменения" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Сохранить изменения" }));
    await waitFor(() => expect(applyChange).toHaveBeenCalledWith("campaign-1", expect.objectContaining({ confirmed_impact: true, comment: null })));
  });

  it("prioritizes strategy review after a strategic campaign change", async () => {
    get.mockResolvedValue({ ...campaign, status: "ACTIVE", strategy_version: 1 });
    workspace.mockResolvedValue({ campaign, director: { strategy_status: "APPROVED", article_count: 3, approved_article_count: 3, plan_status: "APPROVED", plan_item_count: 16, plan_items_with_posts: 2, plan_items_without_posts: 14, social_post_count: 2, posts_waiting_approval: 0, scheduled_publication_count: 1, published_count: 3, failed_task_count: 0, blocked_task_count: 0, next_step: { title: "Обновить стратегию кампании", description: "Маркетинговые вводные изменились после утверждения стратегии v1.", href: "#campaign-change", entity_type: "campaign_change", entity_id: campaign.id, priority: "HIGH" } }, knowledge: { store_ready: true, source_count: 0, item_count: 0, ready_item_count: 0, processing_item_count: 0, failed_item_count: 0, campaign_packs: [], has_current_strategy_pack: true }, articles: [], social_posts: [], publication_plan: null, other_plans: [], publications: [], attention_tasks: [], feedback: { new_feedback_count: 0, new_metrics_count: 0 }, change_state: { has_pending_strategic_changes: true, changed_fields: ["target_audience"], changed_at: "2026-10-02T10:00:00Z", changed_by_user_id: "user-1", comment: null, baseline_strategy_version: 1, active_strategy_version: 1, plan_requires_review: true, affected_article_count: 3, affected_social_post_count: 2 } });
    prepareStrategyRevision.mockResolvedValue({ status: "PLANNING" });
    render(<CampaignDetailsPage />);
    expect(await screen.findByRole("heading", { name: "Обновить стратегию кампании" })).toBeInTheDocument();
    expect(screen.getByText(/Текущая стратегия остаётся source of truth/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Подготовить новую версию стратегии" }));
    await waitFor(() => expect(prepareStrategyRevision).toHaveBeenCalledWith("campaign-1"));
  });

  it("shows readable invalid date error", async () => {
    render(<NewCampaignPage />);
    fireEvent.change(screen.getByLabelText("Название *"), { target: { value: "Campaign" } });
    fireEvent.change(screen.getByLabelText("Цель *"), { target: { value: "Goal" } });
    fireEvent.change(screen.getByLabelText("Дата начала"), { target: { value: "2026-10-31" } });
    fireEvent.change(screen.getByLabelText("Дата окончания"), { target: { value: "2026-10-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Создать кампанию" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Дата окончания не может быть раньше даты начала.");
  });

  it("generates a strategy from DRAFT", async () => {
    generateStrategy.mockResolvedValue({ status: "PLANNING" });
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Сформировать стратегию" }));
    await waitFor(() => expect(generateStrategy).toHaveBeenCalledWith("campaign-1"));
  });

  it("renders plan, approves, and submits revision feedback", async () => {
    const strategy = {
      campaign_summary: "Резюме кампании", positioning: "Позиционирование", target_audience: "CEO",
      main_message: "Главное сообщение", content_strategy: "Контентная стратегия",
      content_topics: ["Тема 1", "Тема 2", "Тема 3"],
      recommended_article: { title: "Статья", objective: "Цель", angle: "Ракурс", cta: "CTA" },
      social_strategy: { channels: ["TELEGRAM", "VK"], post_count: 5, approach: "Подход" },
      tasks: [{ key: "research", task_type: "KNOWLEDGE_RESEARCH", title: "Найти знания", description: "Описание", agent_slug: "knowledge_keeper", priority: "NORMAL", brief: "Brief", depends_on: [] }],
    };
    get.mockResolvedValue({ ...campaign, status: "WAITING_APPROVAL", strategy_version: 1, strategy });
    approvalList.mockResolvedValue([{ id: "approval-1", object_type: "CAMPAIGN_STRATEGY", object_id: campaign.id, subject_version: 1, status: "PENDING", reviewed_by_user_id: null, comment: null, subject_snapshot: strategy, metadata: {}, created_at: campaign.created_at, resolved_at: null, updated_at: campaign.updated_at }]);
    approveStrategy.mockResolvedValue({});
    requestRevision.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Резюме кампании")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Утвердить стратегию" }));
    await waitFor(() => expect(approveStrategy).toHaveBeenCalledWith("campaign-1"));
    fireEvent.click(screen.getByRole("button", { name: "Запросить доработку" }));
    fireEvent.change(screen.getByLabelText("Что необходимо изменить?"), { target: { value: "Усилить фокус" } });
    fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(requestRevision).toHaveBeenCalledWith("campaign-1", "Усилить фокус"));
  });

  it("shows real campaign content and pending approval count", async () => {
    contentList.mockResolvedValue([
      { id: "article-1", campaign_id: campaign.id, content_type: "ARTICLE", title: "Статья", status: "WAITING_APPROVAL", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at },
      { id: "pack-1", campaign_id: campaign.id, content_type: "SOCIAL_POST_PACK", title: "Пакет", status: "WAITING_APPROVAL", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at },
      { id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "WAITING_APPROVAL", parent_content_item_id: "pack-1", channel: "TELEGRAM", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at },
    ]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByRole("heading", { name: "Контент кампании" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Статьи/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Посты для соцсетей/ })).toBeInTheDocument();
    expect(await screen.findByText("Ожидают согласования: 3")).toBeInTheDocument();
    expect(screen.getByText("Пакет")).toBeInTheDocument();
    expect(screen.getByText(/Постов: 1/)).toBeInTheDocument();
    expect(screen.getByText("Пост").closest(".content-card")?.parentElement).toHaveClass("content-card-stack");
  });

  it("renders backend workflow statuses and activity timeline", async () => {
    taskList.mockResolvedValue([
      { id: "knowledge", task_type: "KNOWLEDGE_RESEARCH", title: "Knowledge", status: "COMPLETED" },
      { id: "article", task_type: "WRITE_ARTICLE", title: "Article", status: "COMPLETED" },
      { id: "social", task_type: "CREATE_SOCIAL_POSTS", title: "Social", status: "READY" },
    ]);
    activityList.mockResolvedValue([
      { id: "event-1", event_type: "TASK_AUTO_DISPATCHED", created_at: "2026-09-22T10:00:00Z" },
      { id: "event-2", event_type: "ARTICLE_CREATED", created_at: "2026-09-22T10:01:00Z" },
      { id: "event-3", event_type: "SOCIAL_POST_PACK_CREATED", created_at: "2026-09-22T10:02:00Z" },
      { id: "event-4", event_type: "CONTENT_REVISION_REQUESTED", created_at: "2026-09-22T10:03:00Z" },
    ]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Поиск знаний")).toBeInTheDocument();
    expect(screen.getByText("Написание статьи")).toBeInTheDocument();
    expect(screen.getByText("Создание публикаций")).toBeInTheDocument();
    expect(screen.getByText("Задача автоматически передана исполнителю")).toBeInTheDocument();
    expect(screen.getByText("Создана статья")).toBeInTheDocument();
    expect(screen.getByText("Создан пакет постов")).toBeInTheDocument();
    expect(screen.getByText("Запрошена доработка материала")).toBeInTheDocument();
  });

  it("renders failed and blocked workflow statuses from backend", async () => {
    taskList.mockResolvedValue([
      { id: "knowledge", task_type: "KNOWLEDGE_RESEARCH", title: "Knowledge", status: "FAILED" },
      { id: "article", task_type: "WRITE_ARTICLE", title: "Article", status: "BLOCKED" },
      { id: "social", task_type: "CREATE_SOCIAL_POSTS", title: "Social", status: "BLOCKED" },
    ]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Поиск знаний")).toBeInTheDocument();
    expect(screen.getAllByText("Ошибка").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Заблокирована").length).toBeGreaterThan(0);
  });

  it("shows publication approval separately from content approval", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "DRAFT", scheduled_at: null, approved_for_publish_at: null, approved_for_publish_by: null, external_id: null, external_url: null, published_at: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    publicationApprove.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    expect((await screen.findAllByText("Черновик")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Согласовать публикацию" }));
    await waitFor(() => expect(publicationApprove).toHaveBeenCalledWith("publication-1"));
  });

  it("gates preparation to approved social posts", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    contentGet.mockResolvedValue({ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM", approved_version_id: "version-1", current_version: { id: "version-1" }, versions: [] });
    publicationCreate.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    expect(await screen.findByRole("button", { name: "Подготовить публикацию" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Подготовить публикацию" }));
    await waitFor(() => expect(publicationCreate).toHaveBeenCalledWith({ content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM" }));
  });

  it("shows a visible error when publication preparation is rejected", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 2, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    contentGet.mockResolvedValue({ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 2, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM", approved_version_id: null, current_version: { id: "new-version" }, versions: [] });
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Подготовить публикацию" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("согласованная версия");
    expect(publicationCreate).not.toHaveBeenCalled();
  });

  it("renders reschedule and cancel controls without provider publishing", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "VK" }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "VK", status: "SCHEDULED", scheduled_at: "2026-10-01T10:00:00Z", approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    vi.spyOn(window, "prompt").mockReturnValue("2026-10-02T10:00:00Z");
    publicationSchedule.mockResolvedValue({}); publicationCancel.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    expect((await screen.findAllByText("Запланирована")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Назначить / перенести" }));
    await waitFor(() => expect(publicationSchedule).toHaveBeenCalledWith("publication-1", "2026-10-02T10:00:00.000Z"));
    fireEvent.click(screen.getByRole("button", { name: "Отменить" }));
    await waitFor(() => expect(publicationCancel).toHaveBeenCalledWith("publication-1"));
    expect(screen.queryByRole("button", { name: /Опубликовать/ })).not.toBeInTheDocument();
  });

  it("renders upcoming calendar publications in ascending local time", async () => {
    contentList.mockResolvedValue([]);
    publicationCalendar.mockResolvedValue([
      { publication_id: "late", content_item_id: "post-2", content_version_id: "v2", title: "Позже", channel: "VK", status: "SCHEDULED", scheduled_at: "2026-10-02T10:00:00Z", published_at: null, external_url: null, provider_enabled: true, failure_code: null },
      { publication_id: "early", content_item_id: "post-1", content_version_id: "v1", title: "Раньше", channel: "TELEGRAM", status: "SCHEDULED", scheduled_at: "2026-10-01T10:00:00Z", published_at: null, external_url: null, provider_enabled: true, failure_code: null },
    ]);
    render(<CampaignDetailsPage />);
    const upcoming = await screen.findByRole("list", { name: "Предстоящие публикации" });
    expect(upcoming.textContent?.indexOf("Раньше")).toBeLessThan(upcoming.textContent?.indexOf("Позже") ?? 0);
    expect(publicationCalendar).toHaveBeenCalled();
  });

  it("publishes approved Telegram publication explicitly and exposes retry only for retryable failures", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "APPROVED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    publicationPublishNow.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Опубликовать сейчас" }));
    await waitFor(() => expect(publicationPublishNow).toHaveBeenCalledWith("publication-1"));
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "FAILED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: "TELEGRAM_RATE_LIMIT", failure_message: "Ограничение Telegram", retry_count: 1, retry_allowed: true, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(publicationRetry).toHaveBeenCalledWith("publication-1"));
  });

  it("publishes an approved VK publication through the shared action", async () => {
    contentList.mockResolvedValue([{ id: "post-vk", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "VK-пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "VK" }]);
    publicationList.mockResolvedValue([{ id: "publication-vk", campaign_id: campaign.id, content_item_id: "post-vk", content_version_id: "version-vk", channel: "VK", status: "APPROVED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    publicationPublishNow.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Опубликовать сейчас" }));
    await waitFor(() => expect(publicationPublishNow).toHaveBeenCalledWith("publication-vk"));
  });

  it("hides VK publish action when the provider is disabled", async () => {
    contentList.mockResolvedValue([{ id: "post-vk", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "VK-пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "VK" }]);
    publicationList.mockResolvedValue([{ id: "publication-vk", campaign_id: campaign.id, content_item_id: "post-vk", content_version_id: "version-vk", channel: "VK", provider_enabled: false, status: "APPROVED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Разрешена к публикации")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Опубликовать сейчас" })).not.toBeInTheDocument();
    expect(publicationPublishNow).not.toHaveBeenCalled();
  });

  it("polls PUBLISHING to PUBLISHED without sending twice", async () => {
    const base = { id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] };
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValueOnce([{ ...base, status: "PUBLISHING" }]).mockResolvedValueOnce([{ ...base, status: "PUBLISHED", external_id: "telegram-1", external_url: "https://t.me/1", published_at: campaign.updated_at }]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Отправляется")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("Опубликована").length).toBeGreaterThan(0), { timeout: 8_000 });
  }, 10_000);

  it("polls PUBLISHING to FAILED and stops without retrying", async () => {
    const base = { id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] };
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValueOnce([{ ...base, status: "PUBLISHING", failure_code: null, failure_message: null }]).mockResolvedValueOnce([{ ...base, status: "FAILED", failure_code: "TELEGRAM_BAD_REQUEST", failure_message: "Плохой запрос" }]);
    render(<CampaignDetailsPage />);
    await waitFor(() => expect(screen.getAllByText("Ошибка отправки").length).toBeGreaterThan(0), { timeout: 8_000 });
  }, 10_000);

  it("disables execution for publishing/published and hides retry for non-retryable failures", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "FAILED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: "TELEGRAM_RECONCILIATION_REQUIRED", failure_message: "Требуется проверка", retry_count: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Ошибка отправки")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Опубликовать сейчас" })).not.toBeInTheDocument();
  });

  it("shows reconciliation actions and no retry before operator decision", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "FAILED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: "TELEGRAM_RECONCILIATION_REQUIRED", failure_message: "Требуется проверка", retry_allowed: false, reconciliation_required: true, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Нужно подтвердить результат отправки.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Подтвердить публикацию" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Подтвердить отсутствие публикации" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
  });
});


describe("Campaign plan focus", () => {
  afterEach(() => cleanup());
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue(campaign);
    taskList.mockResolvedValue([]);
    approvalList.mockResolvedValue([]);
    contentList.mockResolvedValue([{ id: "article-1", campaign_id: campaign.id, content_type: "ARTICLE", title: "Исходная статья", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at }]);
    activityList.mockResolvedValue([]);
    publicationList.mockResolvedValue([]);
    publicationCalendar.mockResolvedValue([]);
    feedbackList.mockResolvedValue([]);
    feedbackAnalyses.mockResolvedValue([]);
    performance.mockResolvedValue(null);
    plansList.mockResolvedValue([]);
  });

  it("renders campaign data and every collapsed plan with defensive date formatting", async () => {
    const makePlan = (id: string, status: "APPROVED" | "DRAFT", topic: string, start: string, end = "2026-11-01T23:59:00Z") => ({
      id,
      campaign_id: campaign.id,
      status,
      planning_horizon_start: start,
      planning_horizon_end: end,
      timezone_policy: "UTC",
      created_by_user_id: "user-1",
      generated_by_agent_run_id: null,
      feedback_analysis_id: null,
      approved_at: null,
      approved_by_user_id: null,
      items: [{
        id: `item-${id}`,
        position: 1,
        scheduled_at: "2026-10-06T12:00:00Z",
        channel: "VK",
        source_content_item_id: "article-1",
        source_content_version_id: "article-version-1",
        topic,
        angle: "Управленческий ракурс",
        purpose: "Помочь выбрать следующий шаг",
        format: "expert_observation",
        message_brief: "Краткий редакционный бриф",
        source_claim_ids: null,
        source_support_summary: null,
        status: "PLANNED",
        near_publication_warnings: [],
      }],
    });
    contentList.mockResolvedValue([
      ...[1, 2, 3].map((number) => ({ id: `article-${number}`, campaign_id: campaign.id, content_type: "ARTICLE", title: `Статья ${number}`, status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at })),
      ...[1, 2].map((number) => ({ id: `post-${number}`, campaign_id: campaign.id, content_type: "SOCIAL_POST", title: `Пост ${number}`, status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "VK" })),
    ]);
    taskList.mockResolvedValue([{ id: "task-1", campaign_id: campaign.id, campaign: { id: campaign.id, name: campaign.name }, task_type: "WRITE_ARTICLE", title: "Подготовить материал", assigned_agent: null, priority: "NORMAL", status: "FAILED", deadline: null, created_at: campaign.created_at, updated_at: campaign.updated_at }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "post-version-1", channel: "VK", status: "SCHEDULED", scheduled_at: "2026-10-06T12:00:00Z", approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    plansList.mockResolvedValue([
      makePlan("draft-old", "DRAFT", "Черновой пункт A", "2026-10-05T00:00:00Z"),
      makePlan("approved", "APPROVED", "Утверждённый пункт", "2026-10-05T00:00:00Z"),
      makePlan("draft-invalid", "DRAFT", "Черновой пункт с датой", "исторически-некорректная-дата"),
      makePlan("draft-new", "DRAFT", "Черновой пункт B", "2026-10-01"),
    ]);

    render(<CampaignDetailsPage />);

    expect(await screen.findByRole("heading", { name: "Утверждённый пункт" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Статьи 3/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Посты для соцсетей 2/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Предстоящие публикации" })).toBeInTheDocument();
    expect(await screen.findAllByText("Подготовить материал")).toHaveLength(1);
    expect(screen.getByRole("list", { name: "Предстоящие публикации" })).toHaveTextContent("Пост 1");
    const otherPlansSummary = screen.getByText("Другие планы (3)");
    const otherPlansDisclosure = otherPlansSummary.closest("details");
    expect(otherPlansDisclosure).not.toHaveAttribute("open");
    expect(otherPlansDisclosure).toHaveTextContent("Некорректная дата");
    fireEvent.click(otherPlansSummary);
    expect(otherPlansDisclosure).toHaveAttribute("open");
    expect(await screen.findByRole("heading", { name: "Черновой пункт A" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Черновой пункт B" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Черновой пункт с датой" })).toBeInTheDocument();
  });
});

describe("Plan-bound publication scheduling", () => {
  const planned = { id: "post-plan", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Плановый пост", status: "APPROVED", current_version_number: 1, current_version_id: "exact-v1", channel: "VK", publication_plan_item_id: "plan-item-2", plan_channel: "VK", plan_scheduled_at: "2026-10-06T12:00:00Z", created_at: campaign.created_at, updated_at: campaign.updated_at };
  const scheduled = { id: "scheduled-1", content_item_id: planned.id, content_version_id: "exact-v1", channel: "VK", status: "SCHEDULED", scheduled_at: planned.plan_scheduled_at, publication_plan_item_id: "plan-item-2" };
  beforeEach(() => { vi.clearAllMocks(); get.mockResolvedValue(campaign); taskList.mockResolvedValue([]); approvalList.mockResolvedValue([]); activityList.mockResolvedValue([]); contentList.mockResolvedValue([planned]); publicationList.mockResolvedValue([]); publicationCalendar.mockResolvedValue([]); plansList.mockResolvedValue([]); });
  afterEach(() => cleanup());

  it("shows browser-local plan context and sends only content ID; displays scheduled calendar", async () => {
    const { formatDateTime } = await import("../lib/campaigns");
    publicationScheduleContent.mockImplementation(async () => { publicationList.mockResolvedValue([scheduled]); publicationCalendar.mockResolvedValue([{ ...scheduled, publication_id: scheduled.id, title: planned.title }]); return scheduled; });
    render(<CampaignDetailsPage />);
    expect(await screen.findByText(`План: VK · ${formatDateTime(planned.plan_scheduled_at)}`)).toBeInTheDocument();
    expect(screen.getByText("Утверждён")).toBeInTheDocument();
    expect(screen.getByText("Публикация ещё не назначена.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Запланировать публикацию" }));
    await waitFor(() => expect(publicationScheduleContent).toHaveBeenCalledWith(planned.id));
    expect(await screen.findByText(`Публикация запланирована на ${formatDateTime(planned.plan_scheduled_at)} · VK`)).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Предстоящие публикации" })).toHaveTextContent(planned.title);
    expect(screen.queryByRole("button", { name: "Запланировать публикацию" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Опубликовать сейчас" })).not.toBeInTheDocument();
    expect(publicationCreate).not.toHaveBeenCalled(); expect(publicationPublishNow).not.toHaveBeenCalled();
    const [, from, to] = publicationCalendar.mock.calls[0];
    expect(new Date(to).getTime()).toBeGreaterThan(Date.now());
    expect(new Date(to).getTime()-new Date(from).getTime()).toBeLessThan(90*86400000);
  });

  it("does not offer duplicate scheduling for exact version already scheduled", async () => {
    publicationList.mockResolvedValue([scheduled]); render(<CampaignDetailsPage />);
    expect(await screen.findByText(/Публикация запланирована на/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Запланировать публикацию" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Назначить \/ перенести" })).not.toBeInTheDocument();
  });

  it("explains overdue schedule without publishing", async () => {
    const message="Плановая дата уже прошла. Выберите отдельное действие для публикации сейчас или измените план.";
    publicationScheduleContent.mockRejectedValue(new Error(message)); render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Запланировать публикацию" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(publicationPublishNow).not.toHaveBeenCalled();
  });

  it("keeps an older scheduled version visible and routes to controlled replacement", async () => {
    contentList.mockResolvedValue([{ ...planned, current_version_id: "exact-v2", approved_version_id: "exact-v2", current_version_number: 2 }]);
    publicationList.mockResolvedValue([scheduled]); render(<CampaignDetailsPage />);
    expect(await screen.findByRole("link", { name: "Проверить версии и разрешённую замену" })).toHaveAttribute("href", `/content/${planned.id}#publication`);
    expect(screen.queryByRole("button", { name: "Запланировать публикацию" })).not.toBeInTheDocument();
    expect(publicationScheduleContent).not.toHaveBeenCalled();
  });

  it("does not treat a published post as scheduled", async () => {
    publicationList.mockResolvedValue([{ ...scheduled, status: "PUBLISHED" }]);
    publicationCalendar.mockResolvedValue([{ ...scheduled, publication_id: scheduled.id, title: planned.title, status: "PUBLISHED" }]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Запланированных публикаций нет.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Запланировать публикацию" })).not.toBeInTheDocument();
  });
  it.each([true, false])("offers explicit overdue plan publish only after human confirmation (%s)", async (confirm) => {
    publicationList.mockResolvedValue([{ ...scheduled, is_overdue: true, lateness_seconds: 7200, provider_enabled: true }]);
    vi.spyOn(window, "confirm").mockReturnValue(confirm);
    publicationPublishNow.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    const button = await screen.findByRole("button", { name: "Опубликовать сейчас" });
    expect(screen.queryByRole("button", { name: "Назначить / перенести" })).not.toBeInTheDocument();
    fireEvent.click(button);
    expect(window.confirm).toHaveBeenCalled();
    expect(screen.queryByText("Запланированных публикаций нет.")).not.toBeInTheDocument();
    if (confirm) await waitFor(() => expect(publicationPublishNow).toHaveBeenCalledWith(scheduled.id));
    else expect(publicationPublishNow).not.toHaveBeenCalled();
    expect(publicationSchedule).not.toHaveBeenCalled();
  });

});
