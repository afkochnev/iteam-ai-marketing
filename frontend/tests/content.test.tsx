import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ContentDetailPage from "../app/content/[id]/page";
import ContentPage from "../app/content/page";
import ApprovalsPage from "../app/approvals/page";

const mocks = vi.hoisted(() => ({ get: vi.fn(), list: vi.fn(), approve: vi.fn(), reject: vi.fn(), requestRevision: vi.fn() }));
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "content-1" }) }));
vi.mock("@/lib/api", async (original) => { const actual = await original<typeof import("@/lib/api")>(); return { ...actual, contentApi: { get: mocks.get, list: mocks.list, approve: mocks.approve, reject: mocks.reject, requestRevision: mocks.requestRevision }, approvalsApi: { list: mocks.list } }; });

const article = { id: "content-1", campaign_id: "campaign-1", source_task_id: "task-1", author_agent_id: "writer-1", content_type: "ARTICLE" as const, title: "Статья", status: "WAITING_APPROVAL" as const, current_version_number: 1, created_at: "2026-09-22T10:00:00Z", updated_at: "2026-09-22T10:00:00Z", current_version: { id: "version-1", version_number: 1, change_description: null, created_at: "2026-09-22T10:00:00Z", content: "# Заголовок\n\n<script>alert(1)</script>", structured_content: {}, sources: [] }, versions: [] };

describe("Content and approvals UI", () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.get.mockResolvedValue(article); mocks.list.mockResolvedValue([{ id: "approval-1", object_type: "CONTENT_ITEM", object_id: "content-1", subject_version: 1, status: "PENDING", reviewed_by_user_id: null, comment: null, subject_snapshot: {}, metadata: {}, created_at: article.created_at, resolved_at: null, updated_at: article.updated_at }]); });
  it("renders article safely and exposes approval actions", async () => { render(<ContentDetailPage />); expect(await screen.findByText("Статья")).toBeInTheDocument(); expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument(); fireEvent.click(screen.getByRole("button", { name: "Утвердить" })); await waitFor(() => expect(mocks.approve).toHaveBeenCalledWith("content-1")); });
  it("requests revision with a required comment", async () => { window.prompt = vi.fn().mockReturnValue("Усилить CTA"); mocks.requestRevision.mockResolvedValue(article); render(<ContentDetailPage />); await screen.findByText("Статья"); fireEvent.click(screen.getByRole("button", { name: "Запросить доработку" })); await waitFor(() => expect(mocks.requestRevision).toHaveBeenCalledWith("content-1", "Усилить CTA")); });
  it("renders content list and approval list", async () => { mocks.list.mockResolvedValueOnce([article]).mockResolvedValueOnce([{ id: "approval-1", object_type: "CONTENT_ITEM", object_id: "content-1", subject_version: 1, status: "PENDING", reviewed_by_user_id: null, comment: null, subject_snapshot: {}, metadata: {}, created_at: article.created_at, resolved_at: null, updated_at: article.updated_at }]); render(<ContentPage />); expect(await screen.findByText("Статья")).toBeInTheDocument(); });
  it("shows pending approval records", async () => { render(<ApprovalsPage />); expect(await screen.findByText("CONTENT_ITEM")).toBeInTheDocument(); expect(screen.getByText("PENDING")).toBeInTheDocument(); });
  it("renders social pack channels, order and provenance sections", async () => {
    mocks.get.mockResolvedValue({ ...article, content_type: "SOCIAL_POST_PACK", title: "Пакет", current_version: { ...article.current_version, content: "# Пакет", structured_content: { strategy_summary: "Стратегия", posts: [{ key: "post_1", channel: "TELEGRAM", title: "Пост", text_markdown: "Текст", cta: "CTA", suggested_publish_order: 1, sources: [{ content_version_id: "article-v1", section_key: "problem" }] }] } } });
    render(<ContentDetailPage />);
    expect(await screen.findByText("Публикации")).toBeInTheDocument();
    expect(screen.getByText("TELEGRAM")).toBeInTheDocument();
    expect(screen.getByText("Основано на разделах статьи: problem")).toBeInTheDocument();
  });
  it("does not offer revision on an individual social post", async () => {
    mocks.get.mockResolvedValue({ ...article, content_type: "SOCIAL_POST", title: "Пост", current_version: { ...article.current_version, content: "Текст" } });
    render(<ContentDetailPage />);
    expect(await screen.findByText("Пост")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Запросить доработку" })).not.toBeInTheDocument();
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
    expect(screen.getByText(/REVISION_REQUESTED/)).toBeInTheDocument();
    expect(screen.getByText(/PENDING/)).toBeInTheDocument();
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
    expect(screen.getByText(/APPROVED/)).toBeInTheDocument();
    expect(screen.getByText(/Обновить серию/)).toBeInTheDocument();
    expect(screen.queryByText("post_05")).not.toBeInTheDocument();
  });
});
