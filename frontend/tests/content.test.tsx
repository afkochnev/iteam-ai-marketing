import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ContentDetailPage from "../app/content/[id]/page";
import ContentPage from "../app/content/page";
import ApprovalsPage from "../app/approvals/page";

const mocks = vi.hoisted(() => ({ get: vi.fn(), list: vi.fn(), approve: vi.fn(), reject: vi.fn(), requestRevision: vi.fn(), campaignGet: vi.fn(), campaignsList: vi.fn(), taskGet: vi.fn(), taskList: vi.fn(), runList: vi.fn(), plansList: vi.fn(), publicationList: vi.fn(), scheduleContent: vi.fn() }));
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "content-1" }), useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/components/auth-provider", () => ({ useAuth: () => ({ user: { role: "ADMIN" }, loading: false }) }));
vi.mock("@/lib/api", async (original) => { const actual = await original<typeof import("@/lib/api")>(); return { ...actual, campaignsApi: { ...actual.campaignsApi, get: mocks.campaignGet, list: mocks.campaignsList }, tasksApi: { ...actual.tasksApi, get: mocks.taskGet, list: mocks.taskList }, agentRunsApi: { ...actual.agentRunsApi, list: mocks.runList }, publicationPlansApi: { ...actual.publicationPlansApi, list: mocks.plansList }, publicationsApi: { ...actual.publicationsApi, listCampaign: mocks.publicationList, scheduleContent: mocks.scheduleContent }, contentApi: { get: mocks.get, list: mocks.list, approve: mocks.approve, reject: mocks.reject, requestRevision: mocks.requestRevision }, approvalsApi: { list: mocks.list } }; });

const article = { id: "content-1", campaign_id: "campaign-1", source_task_id: "task-1", author_agent_id: "writer-1", content_type: "ARTICLE" as const, title: "Статья", status: "WAITING_APPROVAL" as const, current_version_number: 1, created_at: "2026-09-22T10:00:00Z", updated_at: "2026-09-22T10:00:00Z", current_version: { id: "version-1", version_number: 1, change_description: null, created_at: "2026-09-22T10:00:00Z", content: "# Заголовок\n\n<script>alert(1)</script>", structured_content: {}, sources: [] }, versions: [] };

describe("Content and approvals UI", () => {
  beforeEach(() => { vi.clearAllMocks(); window.history.replaceState({}, "", "/"); mocks.get.mockResolvedValue(article); mocks.list.mockResolvedValue([{ id: "approval-1", object_type: "CONTENT_ITEM", object_id: "content-1", subject_version: 1, status: "PENDING", reviewed_by_user_id: null, comment: null, subject_snapshot: {}, metadata: {}, created_at: article.created_at, resolved_at: null, updated_at: article.updated_at }]); mocks.campaignGet.mockResolvedValue({ id: "campaign-1", name: "Кампания" }); mocks.campaignsList.mockResolvedValue([]); mocks.taskGet.mockResolvedValue({ id: "task-1", title: "Задача", task_type: "WRITE_ARTICLE" }); mocks.taskList.mockResolvedValue([]); mocks.runList.mockResolvedValue([]); mocks.plansList.mockResolvedValue([]); mocks.publicationList.mockResolvedValue([]); });
  it("renders article safely and exposes approval actions", async () => { render(<ContentDetailPage />); expect(await screen.findByRole("heading", { name: "Статья" })).toBeInTheDocument(); expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument(); fireEvent.click(screen.getByRole("button", { name: "Утвердить материал" })); await waitFor(() => expect(mocks.approve).toHaveBeenCalledWith("content-1")); });
  it("requests revision with a required comment", async () => { window.prompt = vi.fn().mockReturnValue("Усилить CTA"); mocks.requestRevision.mockResolvedValue(article); render(<ContentDetailPage />); await screen.findByRole("heading", { name: "Статья" }); fireEvent.click(screen.getByRole("button", { name: "Запросить доработку" })); await waitFor(() => expect(mocks.requestRevision).toHaveBeenCalledWith("content-1", "Усилить CTA")); });
  it("renders content list and approval list", async () => { mocks.list.mockResolvedValueOnce([article]).mockResolvedValueOnce([{ id: "approval-1", object_type: "CONTENT_ITEM", object_id: "content-1", subject_version: 1, status: "PENDING", reviewed_by_user_id: null, comment: null, subject_snapshot: {}, metadata: {}, created_at: article.created_at, resolved_at: null, updated_at: article.updated_at }]); render(<ContentPage />); expect(await screen.findByText("Статья")).toBeInTheDocument(); });
  it("applies campaign and content-type filters from campaign links", async () => {
    window.history.replaceState({}, "", "/content?campaign_id=campaign-1&content_type=ARTICLE");
    mocks.list.mockResolvedValue([{ ...article, content_type: "ARTICLE" }, { ...article, id: "post-1", title: "Пост", content_type: "SOCIAL_POST" }]);
    mocks.campaignsList.mockResolvedValue([{ id: "campaign-1", name: "Кампания" }]);
    render(<ContentPage />);
    expect(await screen.findByText("Статья")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Тип материала")).toHaveValue("ARTICLE");
      expect(screen.getByLabelText("Кампания")).toHaveValue("campaign-1");
    });
    expect(screen.queryByText("Пост")).not.toBeInTheDocument();
  });
  it("shows pending approval records with human-readable labels", async () => { render(<ApprovalsPage />); expect(await screen.findByText("Открыть материал")).toBeInTheDocument(); expect(screen.getByText("Ожидает решения")).toBeInTheDocument(); });
  it("renders social pack channels, order and provenance sections", async () => {
    mocks.get.mockResolvedValue({ ...article, content_type: "SOCIAL_POST_PACK", title: "Пакет", current_version: { ...article.current_version, content: "# Пакет", structured_content: { strategy_summary: "Стратегия", posts: [{ key: "post_1", channel: "TELEGRAM", title: "Пост", text_markdown: "Текст", cta: "CTA", suggested_publish_order: 1, sources: [{ content_version_id: "article-v1", section_key: "problem" }] }] } } });
    render(<ContentDetailPage />);
    expect(await screen.findByText("Пакет публикаций")).toBeInTheDocument();
    expect(screen.getByText("Telegram")).toBeInTheDocument();
    expect(screen.getByText("Основано на разделах статьи: problem")).toBeInTheDocument();
  });
  it("does not offer revision on an individual social post", async () => {
    mocks.get.mockResolvedValue({ ...article, content_type: "SOCIAL_POST", title: "Пост", current_version: { ...article.current_version, content: "Текст" } });
    render(<ContentDetailPage />);
    expect(await screen.findByRole("heading", { name: "Пост" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Запросить доработку" })).not.toBeInTheDocument();
  });

  it("shows the approved post version, article provenance and its plan-owned schedule action", async () => {
    const articleVersion = "article-version-1";
    const postVersion = "post-version-1";
    mocks.get.mockResolvedValue({
      ...article,
      id: "post-1",
      content_type: "SOCIAL_POST",
      title: "Пост из статьи",
      status: "APPROVED",
      channel: "VK",
      publication_plan_item_id: "plan-item-1",
      approved_version_id: postVersion,
      current_version: { ...article.current_version, id: postVersion, content: "Текст поста", version_number: 1, derivations: [{ source_content_item_id: "article-1", source_content_item_title: "Исходная статья", source_content_version_id: articleVersion, source_version_number: 2, section_key: "problem" }] },
      versions: [{ id: postVersion, version_number: 1, created_at: article.created_at, change_description: null }],
    });
    mocks.plansList.mockResolvedValue([{ id: "plan-1", campaign_id: "campaign-1", status: "APPROVED", planning_horizon_start: article.created_at, planning_horizon_end: article.updated_at, timezone_policy: "browser", created_by_user_id: "user-1", generated_by_agent_run_id: null, feedback_analysis_id: null, approved_at: article.updated_at, approved_by_user_id: "user-1", items: [{ id: "plan-item-1", position: 1, scheduled_at: "2026-10-03T10:00:00Z", channel: "VK", source_content_item_id: "article-1", source_content_version_id: articleVersion, topic: "Проблема", angle: "Практический вывод", purpose: "Объяснить", format: "post", message_brief: "Бриф", source_claim_ids: ["claim-1"], source_support_summary: "Тезис из статьи", status: "PLANNED", near_publication_warnings: [] }] }]);
    mocks.scheduleContent.mockResolvedValue({ id: "publication-1", content_item_id: "post-1", content_version_id: postVersion, channel: "VK", status: "SCHEDULED", scheduled_at: "2026-10-03T10:00:00Z" });
    render(<ContentDetailPage />);
    expect((await screen.findAllByRole("link", { name: "Исходная статья" })).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: "Исходная статья" })[0]).toHaveAttribute("href", "/content/article-1");
    expect(screen.getByText(/Статья · версия 2 · раздел «problem»/)).toBeInTheDocument();
    expect(screen.getByText("Утверждённая версия").parentElement).toHaveTextContent("v1");
    expect(screen.getByText("claim-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Запланировать публикацию" }));
    await waitFor(() => expect(mocks.scheduleContent).toHaveBeenCalledWith("post-1"));
  });

  it("renders article revision history with the current version", async () => {
    mocks.get.mockResolvedValue({
      ...article,
      status: "WAITING_APPROVAL",
      current_version_number: 2,
      current_version: { ...article.current_version, id: "version-2", version_number: 2, content: "# Новая версия" },
      versions: [
        { id: "version-1", version_number: 1, change_description: "Initial", created_at: article.created_at },
        { id: "version-2", version_number: 2, change_description: "Revision", created_at: "2026-09-23T10:00:00Z" },
      ],
      approval_history: [
        { id: "approval-1", subject_version: 1, status: "REVISION_REQUESTED", comment: "Усилить CTA", reviewed_by_user_id: "user-1", created_at: article.created_at, resolved_at: article.created_at },
        { id: "approval-2", subject_version: 2, status: "PENDING", comment: null, reviewed_by_user_id: null, created_at: "2026-09-23T10:00:00Z", resolved_at: null },
      ],
    });
    render(<ContentDetailPage />);
    expect(await screen.findByText("Новая версия")).toBeInTheDocument();
    expect(screen.getAllByText(/Версия 1/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/Версия 2/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/запрошена доработка/)).toBeInTheDocument();
    expect(screen.getByText(/ожидает решения/)).toBeInTheDocument();
    expect(screen.getByText(/Усилить CTA/)).toBeInTheDocument();
  });

  it("renders revised pack history and keeps removed posts out of current pack", async () => {
    mocks.get.mockResolvedValue({
      ...article,
      content_type: "SOCIAL_POST_PACK",
      title: "Пакет",
      current_version_number: 2,
      current_version: { ...article.current_version, id: "pack-v2", version_number: 2, content: "# Пакет", structured_content: { strategy_summary: "v2", posts: [{ key: "post_06", channel: "VK", title: "Новый пост", text_markdown: "Текст", cta: "CTA", suggested_publish_order: 1, sources: [] }] } },
      versions: [
        { id: "pack-v1", version_number: 1, change_description: "Initial", created_at: article.created_at },
        { id: "pack-v2", version_number: 2, change_description: "Revision", created_at: "2026-09-23T10:00:00Z" },
      ],
      approval_history: [
        { id: "pack-approval-1", subject_version: 1, status: "REVISION_REQUESTED", comment: "Обновить серию", reviewed_by_user_id: "user-1", created_at: article.created_at, resolved_at: article.created_at },
        { id: "pack-approval-2", subject_version: 2, status: "APPROVED", comment: "Согласовано", reviewed_by_user_id: "user-1", created_at: "2026-09-23T10:00:00Z", resolved_at: "2026-09-23T10:01:00Z" },
      ],
    });
    render(<ContentDetailPage />);
    expect(await screen.findByText("Новый пост")).toBeInTheDocument();
    expect(screen.getAllByText(/Версия 1/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/Версия 2/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/утверждена/)).toBeInTheDocument();
    expect(screen.getByText(/Обновить серию/)).toBeInTheDocument();
    expect(screen.queryByText("post_05")).not.toBeInTheDocument();
  });
});
