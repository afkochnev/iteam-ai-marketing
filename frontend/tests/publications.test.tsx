import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PublicationsPage from "../app/publications/page";

const mocks = vi.hoisted(() => ({ campaigns: vi.fn(), calendar: vi.fn(), publications: vi.fn(), content: vi.fn(), plans: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/components/auth-provider", () => ({ useAuth: () => ({ user: { role: "ADMIN" }, loading: false }) }));
vi.mock("@/lib/api", async (original) => {
  const actual = await original<typeof import("@/lib/api")>();
  return { ...actual, campaignsApi: { ...actual.campaignsApi, list: mocks.campaigns }, contentApi: { ...actual.contentApi, list: mocks.content }, publicationPlansApi: { ...actual.publicationPlansApi, list: mocks.plans }, publicationsApi: { ...actual.publicationsApi, calendar: mocks.calendar, listCampaign: mocks.publications } };
});

describe("publication calendar", () => {
  beforeEach(() => {
    vi.clearAllMocks(); mocks.publications.mockResolvedValue([]); mocks.content.mockResolvedValue([]); mocks.plans.mockResolvedValue([]);
    mocks.campaigns.mockResolvedValue([{ id: "campaign-1", name: "Запуск" }]);
    mocks.calendar.mockResolvedValue([
      { publication_id: "next", content_item_id: "post-1", content_version_id: "v1", title: "Ближайший пост", channel: "VK", status: "SCHEDULED", scheduled_at: "2026-10-03T10:00:00Z", published_at: null, external_url: null, provider_enabled: true, failure_code: null },
      { publication_id: "done", content_item_id: "post-2", content_version_id: "v2", title: "Опубликованный пост", channel: "TELEGRAM", status: "PUBLISHED", scheduled_at: null, published_at: "2026-10-01T10:00:00Z", external_url: null, provider_enabled: true, failure_code: null },
    ]);
  });

  it("separates upcoming work from publication history and links back to campaign context", async () => {
    render(<PublicationsPage />);
    expect(await screen.findByRole("heading", { name: "Предстоящие" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ближайший пост" })).toHaveAttribute("href", "/content/post-1");
    expect(screen.getByText("Опубликованные · 1")).toBeInTheDocument();
    expect(screen.getAllByText("Запуск")[0]).toHaveAttribute("href", "/campaigns/campaign-1");
    expect(mocks.calendar).toHaveBeenCalledWith("campaign-1", expect.any(String), expect.any(String));
    const [, from, to] = mocks.calendar.mock.calls[0] as [string, string, string];
    expect(Date.parse(to) - Date.parse(from)).toBe(90 * 24 * 60 * 60 * 1000);
  });
});

describe("publication operator actions", () => {
  beforeEach(() => {
    vi.clearAllMocks(); mocks.campaigns.mockResolvedValue([{ id: "campaign-1", name: "Запуск", status: "ACTIVE" }]); mocks.calendar.mockResolvedValue([]); mocks.publications.mockResolvedValue([]); mocks.content.mockResolvedValue([]); mocks.plans.mockResolvedValue([]);
  });
  it("routes an exact approved unscheduled post to scheduling and cancelled delivery does not block it", async () => {
    mocks.content.mockResolvedValue([{ id: "post-ready", campaign_id: "campaign-1", title: "Готовый пост", content_type: "SOCIAL_POST", status: "APPROVED", current_version_id: "v3", approved_version_id: "v3", approved_version_number: 3, publication_plan_item_id: "plan-item-1", channel: "VK" }]);
    mocks.plans.mockResolvedValue([{ status: "APPROVED", items: [{ id: "plan-item-1", status: "PLANNED" }] }]);
    mocks.publications.mockResolvedValue([{ id: "cancelled", content_item_id: "post-ready", content_version_id: "v2", status: "CANCELLED", channel: "VK", scheduled_at: "2026-10-05T12:00:00Z" }]);
    render(<PublicationsPage />); expect(await screen.findByRole("link", { name: "Открыть пост и запланировать" })).toHaveAttribute("href", "/content/post-ready#publication");
    expect(within(screen.getByRole("region", { name: "Требуют действия / В процессе" })).queryByText("v2")).not.toBeInTheDocument();
    expect(screen.getByText(/утверждена версия v3/)).toBeInTheDocument(); expect(screen.getByText(/Запланированных публикаций пока нет/)).toBeInTheDocument();
  });
  it.each(["DRAFT", "WAITING_APPROVAL", "APPROVED", "PUBLISHING"])("shows %s with exact operational metadata and the existing next step", async (status) => {
    mocks.content.mockResolvedValue([{ id: "post-active", title: "Active post", content_type: "SOCIAL_POST", status: "APPROVED", current_version_id: "exact-v4", approved_version_id: "exact-v4" }]);
    mocks.publications.mockResolvedValue([{ id: "active", content_item_id: "post-active", content_version_id: "exact-v4", status, channel: "VK", scheduled_at: "2026-10-06T12:00:00Z", provider_enabled: false }]);
    render(<PublicationsPage />);
    const section = await screen.findByRole("region", { name: "Требуют действия / В процессе" });
    const row = within(section);
    expect(await row.findByRole("link", { name: "Active post" })).toHaveAttribute("href", "/content/post-active");
    expect(row.getByRole("link", { name: "Запуск" })).toHaveAttribute("href", "/campaigns/campaign-1");
    expect(row.getByText("exact-v4")).toBeInTheDocument();
    expect(row.getByText(/VK ·/)).toBeInTheDocument();
    expect(row.getByText("Автоматическая отправка сейчас отключена")).toBeInTheDocument();
    expect(row.getByText(({ DRAFT: "Черновик", WAITING_APPROVAL: "Ожидает согласования", APPROVED: "Разрешена к публикации", PUBLISHING: "Отправляется" })[status]!)).toBeInTheDocument();
    const name = status === "PUBLISHING" ? "Проверить состояние отправки в кампании" : status === "APPROVED" ? "Открыть кампанию и назначить публикацию" : "Открыть кампанию и согласовать публикацию";
    expect(row.getByRole("link", { name })).toHaveAttribute("href", "/campaigns/campaign-1#publications");
    expect(row.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Открыть пост и запланировать" })).not.toBeInTheDocument();
    if (status === "PUBLISHING") expect(row.getByRole("status")).toHaveTextContent("результат ещё не подтверждён");
  });

  it("shows scheduled exact version and disabled delivery, and includes undated reconciliation failures", async () => {
    mocks.publications.mockResolvedValue([{ id: "scheduled", content_item_id: "post-1", content_version_id: "exact-approved-v2", status: "SCHEDULED", channel: "TELEGRAM", scheduled_at: "2026-10-06T12:00:00Z", provider_enabled: false }, { id: "failed", content_item_id: "post-2", content_version_id: "old-v1", status: "FAILED", channel: "VK", scheduled_at: null, provider_enabled: false, failure_code: "VK_RECONCILIATION_REQUIRED" }]);
    mocks.content.mockResolvedValue([{ id: "post-1", title: "Scheduled title" }, { id: "post-2", title: "Failed title" }]);
    render(<PublicationsPage />); expect(await screen.findByText("exact-approved-v2")).toBeInTheDocument(); expect(screen.getAllByText("Автоматическая отправка сейчас отключена")).toHaveLength(2); expect(screen.getByText("VK_RECONCILIATION_REQUIRED", { exact: false })).toBeInTheDocument(); expect(screen.getByRole("link", { name: /повтор или проверка/ })).toHaveAttribute("href", "/campaigns/campaign-1#publications");
  });
  it("does not imply worker readiness from provider configuration", async () => {
    mocks.calendar.mockResolvedValue([{ publication_id: "s", content_item_id: "p", content_version_id: "v", title: "Post", status: "SCHEDULED", channel: "VK", scheduled_at: "2026-10-06T12:00:00Z", provider_enabled: true }]);
    render(<PublicationsPage />); expect(await screen.findByText(/Готовность автоматической отправки не подтверждена/)).toBeInTheDocument(); expect(screen.queryByRole("button", { name: /Опубликовать/ })).not.toBeInTheDocument();
  });
  it("does not offer scheduling for an unapproved current version or an archived campaign", async () => {
    mocks.content.mockResolvedValue([{ id: "p", title: "Post", content_type: "SOCIAL_POST", status: "APPROVED", current_version_id: "v3", approved_version_id: "v2" }]);
    render(<PublicationsPage />); expect(await screen.findByText(/Готовых к планированию постов пока нет/)).toBeInTheDocument(); expect(screen.queryByRole("link", { name: "Открыть пост и запланировать" })).not.toBeInTheDocument();
  });
  it("offers recovery routes in empty and error states, retrying without reloading the window", async () => {
    mocks.campaigns.mockRejectedValueOnce(new Error("offline"));
    render(<PublicationsPage />); expect(await screen.findByRole("alert")).toHaveTextContent("offline");
    const { fireEvent } = await import("@testing-library/react"); fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByRole("link", { name: "Проверить контент и согласования" })).toHaveAttribute("href", "/content"); expect(mocks.campaigns.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
