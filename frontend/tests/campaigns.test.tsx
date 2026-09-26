import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CampaignDetailsPage from "../app/campaigns/[id]/page";
import EditCampaignPage from "../app/campaigns/[id]/edit/page";
import NewCampaignPage from "../app/campaigns/new/page";
import CampaignsPage from "../app/campaigns/page";

const { replace, push, list, get, create, update, archive, generateStrategy, approveStrategy, requestRevision, rejectStrategy, taskList, approvalList, contentList, contentGet, activityList, publicationList, publicationCreate, publicationApprove, publicationSchedule, publicationCancel, publicationPublishNow, publicationRetry } = vi.hoisted(() => ({
  replace: vi.fn(), push: vi.fn(), list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), archive: vi.fn(), generateStrategy: vi.fn(), approveStrategy: vi.fn(), requestRevision: vi.fn(), rejectStrategy: vi.fn(), taskList: vi.fn(), approvalList: vi.fn(), contentList: vi.fn(), contentGet: vi.fn(), activityList: vi.fn(), publicationList: vi.fn(), publicationCreate: vi.fn(), publicationApprove: vi.fn(), publicationSchedule: vi.fn(), publicationCancel: vi.fn(), publicationPublishNow: vi.fn(), publicationRetry: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push }), useParams: () => ({ id: "campaign-1" }) }));
vi.mock("@/components/auth-provider", () => ({ useAuth: () => ({ user: { role: "ADMIN" }, loading: false }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, campaignsApi: { list, get, create, update, archive, generateStrategy, approveStrategy, requestStrategyRevision: requestRevision, rejectStrategy }, approvalsApi: { list: approvalList, get: vi.fn() }, tasksApi: { ...actual.tasksApi, list: taskList }, contentApi: { ...actual.contentApi, list: contentList, get: contentGet }, activitiesApi: { list: activityList }, publicationsApi: { listCampaign: publicationList, approve: publicationApprove, create: publicationCreate, schedule: publicationSchedule, cancel: publicationCancel, publishNow: publicationPublishNow, retry: publicationRetry } };
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
  afterEach(() => cleanup());
  beforeEach(() => { vi.clearAllMocks(); list.mockResolvedValue([campaign]); get.mockResolvedValue(campaign); taskList.mockResolvedValue([]); approvalList.mockResolvedValue([]); contentList.mockResolvedValue([]); activityList.mockResolvedValue([]); publicationList.mockResolvedValue([]); vi.spyOn(window, "confirm").mockReturnValue(true); });

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
    expect(screen.queryByText("Редактировать")).not.toBeInTheDocument();
  });

  it("keeps archived campaign read-only", async () => {
    get.mockResolvedValue({ ...campaign, status: "ARCHIVED" }); render(<CampaignDetailsPage />);
    expect(await screen.findByText("Архив")).toBeInTheDocument();
    expect(screen.queryByText("Редактировать")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Архивировать" })).not.toBeInTheDocument();
  });

  it("edits campaign and redirects", async () => {
    update.mockResolvedValue({ ...campaign, desired_result: "40 заявок" }); render(<EditCampaignPage />);
    const result = await screen.findByLabelText("Желаемый результат");
    fireEvent.change(result, { target: { value: "40 заявок" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("campaign-1", expect.objectContaining({ desired_result: "40 заявок" })));
    expect(push).toHaveBeenCalledWith("/campaigns/campaign-1");
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
    expect(await screen.findByText("Ожидают согласования: 3")).toBeInTheDocument();
    expect(screen.getByText("Пакет")).toBeInTheDocument();
    expect(screen.getByText(/постов: 1/)).toBeInTheDocument();
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
    expect(await screen.findByText("KNOWLEDGE_RESEARCH: Завершена")).toBeInTheDocument();
    expect(screen.getByText("WRITE_ARTICLE: Завершена")).toBeInTheDocument();
    expect(screen.getByText("CREATE_SOCIAL_POSTS: Готова")).toBeInTheDocument();
    expect(screen.getByText(/TASK_AUTO_DISPATCHED/)).toBeInTheDocument();
    expect(screen.getByText(/ARTICLE_CREATED/)).toBeInTheDocument();
    expect(screen.getByText(/SOCIAL_POST_PACK_CREATED/)).toBeInTheDocument();
    expect(screen.getByText(/CONTENT_REVISION_REQUESTED/)).toBeInTheDocument();
  });

  it("renders failed and blocked workflow statuses from backend", async () => {
    taskList.mockResolvedValue([
      { id: "knowledge", task_type: "KNOWLEDGE_RESEARCH", title: "Knowledge", status: "FAILED" },
      { id: "article", task_type: "WRITE_ARTICLE", title: "Article", status: "BLOCKED" },
      { id: "social", task_type: "CREATE_SOCIAL_POSTS", title: "Social", status: "BLOCKED" },
    ]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("KNOWLEDGE_RESEARCH: Ошибка")).toBeInTheDocument();
    expect(screen.getByText("WRITE_ARTICLE: Заблокирована")).toBeInTheDocument();
    expect(screen.getByText("CREATE_SOCIAL_POSTS: Заблокирована")).toBeInTheDocument();
  });

  it("shows publication approval separately from content approval", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "DRAFT", scheduled_at: null, approved_for_publish_at: null, approved_for_publish_by: null, external_id: null, external_url: null, published_at: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    publicationApprove.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    expect(await screen.findByText("Статус публикации: DRAFT")).toBeInTheDocument();
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
    expect(await screen.findByText(/Статус публикации: SCHEDULED/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Назначить / перенести" }));
    await waitFor(() => expect(publicationSchedule).toHaveBeenCalledWith("publication-1", "2026-10-02T10:00:00Z"));
    fireEvent.click(screen.getByRole("button", { name: "Отменить" }));
    await waitFor(() => expect(publicationCancel).toHaveBeenCalledWith("publication-1"));
    expect(screen.queryByRole("button", { name: /Опубликовать/ })).not.toBeInTheDocument();
  });

  it("publishes approved Telegram publication explicitly and exposes retry only for retryable failures", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "APPROVED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    publicationPublishNow.mockResolvedValue({});
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Опубликовать сейчас" }));
    await waitFor(() => expect(publicationPublishNow).toHaveBeenCalledWith("publication-1"));
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "FAILED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: "TELEGRAM_RATE_LIMIT", failure_message: "Ограничение Telegram", retry_count: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    render(<CampaignDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(publicationRetry).toHaveBeenCalledWith("publication-1"));
  });

  it("polls PUBLISHING to PUBLISHED without sending twice", async () => {
    const base = { id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, failure_code: null, failure_message: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] };
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValueOnce([{ ...base, status: "PUBLISHING" }]).mockResolvedValueOnce([{ ...base, status: "PUBLISHED", external_id: "telegram-1", external_url: "https://t.me/1", published_at: campaign.updated_at }]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText(/Статус публикации: PUBLISHING/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Статус публикации: PUBLISHED/)).toBeInTheDocument(), { timeout: 5_000 });
  });

  it("polls PUBLISHING to FAILED and stops without retrying", async () => {
    const base = { id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, retry_count: 0, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] };
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValueOnce([{ ...base, status: "PUBLISHING", failure_code: null, failure_message: null }]).mockResolvedValueOnce([{ ...base, status: "FAILED", failure_code: "TELEGRAM_BAD_REQUEST", failure_message: "Плохой запрос" }]);
    render(<CampaignDetailsPage />);
    await waitFor(() => expect(screen.getByText(/Статус публикации: FAILED/)).toBeInTheDocument(), { timeout: 5_000 });
  });

  it("disables execution for publishing/published and hides retry for non-retryable failures", async () => {
    contentList.mockResolvedValue([{ id: "post-1", campaign_id: campaign.id, content_type: "SOCIAL_POST", title: "Пост", status: "APPROVED", current_version_number: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, channel: "TELEGRAM" }]);
    publicationList.mockResolvedValue([{ id: "publication-1", campaign_id: campaign.id, content_item_id: "post-1", content_version_id: "version-1", channel: "TELEGRAM", status: "FAILED", scheduled_at: null, approved_for_publish_at: campaign.created_at, approved_for_publish_by: "user-1", external_id: null, external_url: null, published_at: null, failure_code: "TELEGRAM_RECONCILIATION_REQUIRED", failure_message: "Требуется проверка", retry_count: 1, created_at: campaign.created_at, updated_at: campaign.updated_at, provenance: [] }]);
    render(<CampaignDetailsPage />);
    expect(await screen.findByText(/Статус публикации: FAILED/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Опубликовать сейчас" })).not.toBeInTheDocument();
  });
});
