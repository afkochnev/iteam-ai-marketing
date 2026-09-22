import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ContentDetailPage from "../app/content/[id]/page";
import ContentPage from "../app/content/page";
import ApprovalsPage from "../app/approvals/page";

const mocks = vi.hoisted(() => ({ get: vi.fn(), list: vi.fn(), approve: vi.fn(), reject: vi.fn() }));
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "content-1" }) }));
vi.mock("@/lib/api", async (original) => { const actual = await original<typeof import("@/lib/api")>(); return { ...actual, contentApi: { get: mocks.get, list: mocks.list, approve: mocks.approve, reject: mocks.reject }, approvalsApi: { list: mocks.list } }; });

const article = { id: "content-1", campaign_id: "campaign-1", source_task_id: "task-1", author_agent_id: "writer-1", content_type: "ARTICLE" as const, title: "Статья", status: "WAITING_APPROVAL" as const, current_version_number: 1, created_at: "2026-09-22T10:00:00Z", updated_at: "2026-09-22T10:00:00Z", current_version: { id: "version-1", version_number: 1, change_description: null, created_at: "2026-09-22T10:00:00Z", content: "# Заголовок\n\n<script>alert(1)</script>", structured_content: {}, sources: [] }, versions: [] };

describe("Content and approvals UI", () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.get.mockResolvedValue(article); mocks.list.mockResolvedValue([{ id: "approval-1", object_type: "CONTENT_ITEM", object_id: "content-1", subject_version: 1, status: "PENDING", reviewed_by_user_id: null, comment: null, subject_snapshot: {}, metadata: {}, created_at: article.created_at, resolved_at: null, updated_at: article.updated_at }]); });
  it("renders article safely and exposes approval actions", async () => { render(<ContentDetailPage />); expect(await screen.findByText("Статья")).toBeInTheDocument(); expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument(); fireEvent.click(screen.getByRole("button", { name: "Утвердить" })); await waitFor(() => expect(mocks.approve).toHaveBeenCalledWith("content-1")); });
  it("renders content list and approval list", async () => { mocks.list.mockResolvedValueOnce([article]).mockResolvedValueOnce([{ id: "approval-1", object_type: "CONTENT_ITEM", object_id: "content-1", subject_version: 1, status: "PENDING", reviewed_by_user_id: null, comment: null, subject_snapshot: {}, metadata: {}, created_at: article.created_at, resolved_at: null, updated_at: article.updated_at }]); render(<ContentPage />); expect(await screen.findByText("Статья")).toBeInTheDocument(); });
  it("shows pending approval records", async () => { render(<ApprovalsPage />); expect(await screen.findByText("CONTENT_ITEM")).toBeInTheDocument(); expect(screen.getByText("PENDING")).toBeInTheDocument(); });
  it("renders social pack channels, order and provenance sections", async () => {
    mocks.get.mockResolvedValue({ ...article, content_type: "SOCIAL_POST_PACK", title: "Пакет", current_version: { ...article.current_version, content: "# Пакет", structured_content: { strategy_summary: "Стратегия", posts: [{ key: "post_1", channel: "TELEGRAM", title: "Пост", text_markdown: "Текст", cta: "CTA", suggested_publish_order: 1, sources: [{ content_version_id: "article-v1", section_key: "problem" }] }] } } });
    render(<ContentDetailPage />);
    expect(await screen.findByText("Публикации")).toBeInTheDocument();
    expect(screen.getByText("TELEGRAM")).toBeInTheDocument();
    expect(screen.getByText("Основано на разделах статьи: problem")).toBeInTheDocument();
  });
});
