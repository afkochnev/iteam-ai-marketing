import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PublicationsPage from "../app/publications/page";

const mocks = vi.hoisted(() => ({
  user: { role: "ADMIN" },
  router: { replace: vi.fn(), push: vi.fn() },
  searchParams: new URLSearchParams(),
  campaigns: vi.fn(),
  calendar: vi.fn(),
  publications: vi.fn(),
  content: vi.fn(),
  reuse: vi.fn(),
  plans: vi.fn(),
  approve: vi.fn(),
  schedule: vi.fn(),
  replan: vi.fn(),
  cancel: vi.fn(),
  cancelPlanContent: vi.fn(),
  reschedulePlanContent: vi.fn(),
  publishNow: vi.fn(),
  retry: vi.fn(),
  reconcilePublished: vi.fn(),
  reconcileNotPublished: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
  useSearchParams: () => mocks.searchParams,
}));
vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({ user: mocks.user, loading: false }),
}));
vi.mock("@/lib/api", async (original) => {
  const actual = await original<typeof import("@/lib/api")>();
  return {
    ...actual,
    campaignsApi: { ...actual.campaignsApi, list: mocks.campaigns },
    contentApi: { ...actual.contentApi, list: mocks.content, reuse: mocks.reuse },
    publicationPlansApi: { ...actual.publicationPlansApi, list: mocks.plans },
    publicationsApi: {
      ...actual.publicationsApi,
      calendar: mocks.calendar,
      listCampaign: mocks.publications,
      approve: mocks.approve,
      schedule: mocks.schedule, replan: mocks.replan,
      cancel: mocks.cancel,
      cancelPlanContent: mocks.cancelPlanContent,
      reschedulePlanContent: mocks.reschedulePlanContent,
      publishNow: mocks.publishNow,
      retry: mocks.retry,
      reconcilePublished: mocks.reconcilePublished,
      reconcileNotPublished: mocks.reconcileNotPublished,
    },
  };
});

function publication(overrides: Record<string, unknown> = {}) {
  return {
    id: "publication-1",
    campaign_id: "campaign-1",
    content_item_id: "post-1",
    content_version_id: "exact-v1",
    channel: "VK",
    status: "SCHEDULED",
    scheduled_at: "2026-10-25T10:00:00Z",
    approved_for_publish_at: "2026-10-01T10:00:00Z",
    approved_for_publish_by: "user-1",
    external_id: null,
    provider_target_id: null,
    external_url: null,
    published_at: null,
    failure_code: null,
    failure_message: null,
    retry_count: 0,
    retry_allowed: false,
    reconciliation_required: false,
    provider_enabled: false,
    publication_plan_item_id: null,
    is_overdue: false,
    lateness_seconds: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    provenance: [],
    ...overrides,
  };
}

describe("publication calendar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.searchParams = new URLSearchParams();
    mocks.publications.mockResolvedValue([]);
    mocks.content.mockResolvedValue([]);
    mocks.plans.mockResolvedValue([]);
    mocks.campaigns.mockResolvedValue([{ id: "campaign-1", name: "Запуск", status: "ACTIVE" }]);
    mocks.calendar.mockResolvedValue([
      {
        publication_id: "next",
        content_item_id: "post-1",
        content_version_id: "v1",
        title: "Ближайший пост",
        channel: "VK",
        status: "SCHEDULED",
        scheduled_at: "2026-10-25T10:00:00Z",
        published_at: null,
        external_url: null,
        provider_enabled: true,
        failure_code: null,
      },
      {
        publication_id: "done",
        content_item_id: "post-2",
        content_version_id: "v2",
        title: "Опубликованный пост",
        channel: "TELEGRAM",
        status: "PUBLISHED",
        scheduled_at: null,
        published_at: "2026-10-01T10:00:00Z",
        external_url: null,
        provider_enabled: true,
        failure_code: null,
      },
    ]);
  });

  it("separates upcoming work from publication history and links back to campaign context", async () => {
    render(<PublicationsPage />);
    expect(await screen.findByRole("heading", { name: "Предстоящие" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Ближайший пост" })).toHaveAttribute("href", "/content/post-1");
    expect(screen.getByText("Опубликованные · 1")).toBeInTheDocument();
    expect(screen.getAllByText("Запуск")[0]).toHaveAttribute("href", "/campaigns/campaign-1");
    expect(mocks.calendar).toHaveBeenCalledWith("campaign-1", expect.any(String), expect.any(String));
    const [, from, to] = mocks.calendar.mock.calls[0] as [string, string, string];
    expect(Date.parse(to) - Date.parse(from)).toBe(90 * 24 * 60 * 60 * 1000);
  });

  it("filters the canonical page by campaign id", async () => {
    mocks.searchParams = new URLSearchParams("campaign_id=campaign-2");
    mocks.campaigns.mockResolvedValue([
      { id: "campaign-1", name: "Первая", status: "ACTIVE" },
      { id: "campaign-2", name: "Вторая", status: "ACTIVE" },
    ]);
    mocks.calendar.mockResolvedValue([]);
    render(<PublicationsPage />);
    expect(await screen.findByText(/Показана кампания «Вторая»/)).toBeInTheDocument();
    expect(mocks.calendar).toHaveBeenCalledTimes(1);
    expect(mocks.calendar).toHaveBeenCalledWith("campaign-2", expect.any(String), expect.any(String));
    expect(screen.getByRole("link", { name: "Показать все кампании" })).toHaveAttribute("href", "/publications");
  });
});

describe("publication operator actions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.searchParams = new URLSearchParams();
    mocks.campaigns.mockResolvedValue([{ id: "campaign-1", name: "Запуск", status: "ACTIVE" }]);
    mocks.calendar.mockResolvedValue([]);
    mocks.publications.mockResolvedValue([]);
    mocks.content.mockResolvedValue([]);
    mocks.plans.mockResolvedValue([]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("routes an exact approved unscheduled post to content scheduling and cancelled delivery does not block it", async () => {
    mocks.content.mockResolvedValue([{
      id: "post-ready",
      campaign_id: "campaign-1",
      title: "Готовый пост",
      content_type: "SOCIAL_POST",
      status: "APPROVED",
      current_version_id: "v3",
      approved_version_id: "v3",
      approved_version_number: 3,
      publication_plan_item_id: "plan-item-1",
      channel: "VK",
    }]);
    mocks.plans.mockResolvedValue([{ status: "APPROVED", items: [{ id: "plan-item-1", status: "PLANNED" }] }]);
    mocks.publications.mockResolvedValue([publication({
      id: "cancelled",
      content_item_id: "post-ready",
      content_version_id: "v2",
      status: "CANCELLED",
      scheduled_at: "2026-10-05T12:00:00Z",
    })]);
    render(<PublicationsPage />);
    expect(await screen.findByRole("link", { name: "Открыть пост и запланировать" })).toHaveAttribute("href", "/content/post-ready#publication");
    expect(within(screen.getByRole("region", { name: "Требуют действия / В процессе" })).queryByText("v2")).not.toBeInTheDocument();
    expect(screen.getByText(/утверждена версия v3/)).toBeInTheDocument();
  });

  it.each(["DRAFT", "WAITING_APPROVAL"])("offers approval and cancellation directly for %s", async (status) => {
    mocks.content.mockResolvedValue([{ id: "post-active", title: "Active post" }]);
    mocks.publications.mockResolvedValue([publication({
      id: "active",
      content_item_id: "post-active",
      content_version_id: "exact-v4",
      status,
      scheduled_at: null,
    })]);
    mocks.approve.mockResolvedValue(publication({
      id: "active",
      content_item_id: "post-active",
      content_version_id: "exact-v4",
      status: "APPROVED",
      scheduled_at: null,
    }));
    render(<PublicationsPage />);
    const approveButton = await screen.findByRole("button", { name: "Согласовать публикацию" });
    const section = screen.getByRole("region", { name: "Требуют действия / В процессе" });
    expect(within(section).getByRole("button", { name: "Отменить" })).toBeInTheDocument();
    fireEvent.click(approveButton);
    await waitFor(() => expect(mocks.approve).toHaveBeenCalledWith("active"));
  });

  it("offers scheduling and cancellation directly for an approved unbound publication", async () => {
    mocks.content.mockResolvedValue([{ id: "post-active", title: "Active post" }]);
    mocks.publications.mockResolvedValue([publication({
      id: "active",
      content_item_id: "post-active",
      content_version_id: "exact-v4",
      status: "APPROVED",
      scheduled_at: null,
      publication_plan_item_id: null,
    })]);
    mocks.schedule.mockResolvedValue(publication({
      id: "active",
      content_item_id: "post-active",
      content_version_id: "exact-v4",
      status: "SCHEDULED",
      scheduled_at: "2026-10-26T10:00:00Z",
      publication_plan_item_id: null,
    }));
    render(<PublicationsPage />);
    const scheduleButton = await screen.findByRole("button", { name: "Назначить время" });
    fireEvent.click(scheduleButton);
    const input = await screen.findByLabelText("Дата и время публикации");
    fireEvent.change(input, { target: { value: "2026-10-26T10:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Запланировать" }));
    await waitFor(() => expect(mocks.schedule).toHaveBeenCalledWith(
      "active",
      new Date("2026-10-26T10:00").toISOString(),
    ));
  });

  it("makes an archived material publication explicitly cancellation-only", async () => {
    mocks.content.mockResolvedValue([{
      id: "post-archived",
      title: "Архивный пост",
      status: "ARCHIVED",
    }]);
    mocks.publications.mockResolvedValue([publication({
      id: "archived-draft",
      content_item_id: "post-archived",
      status: "DRAFT",
      scheduled_at: null,
    })]);
    render(<PublicationsPage />);
    const section = await screen.findByRole("region", { name: "Требуют действия / В процессе" });
    expect(await within(section).findByRole("alert")).toHaveTextContent("можно только отменить");
    expect(within(section).queryByRole("button", { name: "Согласовать публикацию" })).not.toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Отменить публикацию" })).toBeInTheDocument();
  });

  it("keeps publishing read-only while delivery is in progress", async () => {
    mocks.content.mockResolvedValue([{ id: "post-active", title: "Active post" }]);
    mocks.publications.mockResolvedValue([publication({
      id: "active",
      content_item_id: "post-active",
      status: "PUBLISHING",
      scheduled_at: "2026-10-25T10:00:00Z",
    })]);
    render(<PublicationsPage />);
    expect(await screen.findByText(/результат ещё не подтверждён/)).toHaveAttribute("role", "status");
    const section = screen.getByRole("region", { name: "Требуют действия / В процессе" });
    expect(within(section).queryByRole("button")).not.toBeInTheDocument();
  });

  it("cancels an overdue publication on the canonical page without provider publishing", async () => {
    mocks.content.mockResolvedValue([{ id: "post-1", title: "Просроченный пост" }]);
    mocks.publications.mockResolvedValue([publication({
      id: "overdue",
      is_overdue: true,
      lateness_seconds: 3600,
      scheduled_at: "2026-10-09T12:00:00Z",
      publication_plan_item_id: "plan-item-1",
      provider_enabled: false,
    })]);
    mocks.cancel.mockResolvedValue(publication({
      id: "overdue",
      status: "CANCELLED",
      is_overdue: true,
      lateness_seconds: 3600,
      scheduled_at: "2026-10-09T12:00:00Z",
      publication_plan_item_id: "plan-item-1",
      provider_enabled: false,
    }));
    render(<PublicationsPage />);
    const section = await screen.findByRole("region", { name: "Просрочены / Требуют решения" });
    expect(within(section).queryByRole("button", { name: "Опубликовать сейчас" })).not.toBeInTheDocument();
    fireEvent.click(await within(section).findByRole("button", { name: "Отменить" }));
    await waitFor(() => expect(mocks.cancel).toHaveBeenCalledWith("overdue"));
    expect((await screen.findAllByRole("status")).some((node) => node.textContent?.includes("Публикация отменена"))).toBe(true);
    await waitFor(() => expect(within(section).queryByText("Просроченный пост")).not.toBeInTheDocument());
    const cancelledSection = screen.getByRole("region", { name: "Отменённые" });
    expect(within(cancelledSection).getByText("Просроченный пост")).toBeInTheDocument();
    expect(within(cancelledSection).getByText(/Запись сохранена в истории/)).toBeInTheDocument();
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it("reschedules an overdue approved plan slot before a Publication exists", async () => {
    mocks.content.mockResolvedValue([{
      id: "post-overdue-plan",
      campaign_id: "campaign-1",
      title: "Просроченный плановый пост",
      content_type: "SOCIAL_POST",
      status: "APPROVED",
      current_version_id: "exact-v3",
      approved_version_id: "exact-v3",
      approved_version_number: 3,
      publication_plan_item_id: "plan-item-overdue",
      plan_channel: "TELEGRAM",
      plan_scheduled_at: "2026-10-20T09:00:00Z",
    }]);
    mocks.plans.mockResolvedValue([{
      status: "APPROVED",
      items: [{ id: "plan-item-overdue", status: "PLANNED" }],
    }]);
    mocks.reschedulePlanContent.mockResolvedValue(publication({
      id: "rescheduled",
      content_item_id: "post-overdue-plan",
      content_version_id: "exact-v3",
      channel: "TELEGRAM",
      scheduled_at: "2026-10-30T09:00:00Z",
      publication_plan_item_id: "plan-item-overdue",
    }));
    render(<PublicationsPage />);
    const section = await screen.findByRole("region", { name: "Просрочены / Требуют решения" });
    expect(await within(section).findByText("Просроченный плановый пост")).toBeInTheDocument();
    fireEvent.click(within(section).getByRole("button", { name: "Перенести" }));
    const input = within(section).getByLabelText("Дата и время публикации");
    fireEvent.change(input, { target: { value: "2026-10-30T09:00" } });
    fireEvent.click(within(section).getByRole("button", { name: "Сохранить перенос" }));
    await waitFor(() => expect(mocks.reschedulePlanContent).toHaveBeenCalledWith(
      "post-overdue-plan",
      new Date("2026-10-30T09:00").toISOString(),
    ));
  });

  it("cancels an overdue approved plan slot before a Publication exists", async () => {
    mocks.content.mockResolvedValue([{
      id: "post-overdue-plan",
      campaign_id: "campaign-1",
      title: "Просроченный плановый пост",
      content_type: "SOCIAL_POST",
      status: "APPROVED",
      current_version_id: "exact-v3",
      approved_version_id: "exact-v3",
      approved_version_number: 3,
      publication_plan_item_id: "plan-item-overdue",
      plan_channel: "TELEGRAM",
      plan_scheduled_at: "2026-10-20T09:00:00Z",
    }]);
    mocks.plans.mockResolvedValue([{
      status: "APPROVED",
      items: [{ id: "plan-item-overdue", status: "PLANNED" }],
    }]);
    mocks.cancelPlanContent.mockResolvedValue(publication({
      id: "cancelled-slot",
      content_item_id: "post-overdue-plan",
      content_version_id: "exact-v3",
      channel: "TELEGRAM",
      status: "CANCELLED",
      scheduled_at: "2026-10-20T09:00:00Z",
      publication_plan_item_id: "plan-item-overdue",
    }));

    render(<PublicationsPage />);
    const section = await screen.findByRole("region", { name: "Просрочены / Требуют решения" });
    fireEvent.click(await within(section).findByRole("button", { name: "Отменить слот" }));
    await waitFor(() => expect(mocks.cancelPlanContent).toHaveBeenCalledWith(
      "post-overdue-plan",
    ));
    await waitFor(() => expect(
      within(section).queryByText("Просроченный плановый пост"),
    ).not.toBeInTheDocument());
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it("keeps a cancelled exact plan slot in history without reopening it", async () => {
    mocks.content.mockResolvedValue([{
      id: "post-cancelled-plan",
      campaign_id: "campaign-1",
      title: "Отменённый плановый пост",
      content_type: "SOCIAL_POST",
      status: "APPROVED",
      current_version_id: "exact-v3",
      approved_version_id: "exact-v3",
      approved_version_number: 3,
      publication_plan_item_id: "plan-item-cancelled",
      plan_channel: "TELEGRAM",
      plan_scheduled_at: "2026-10-20T09:00:00Z",
    }]);
    mocks.plans.mockResolvedValue([{
      status: "APPROVED",
      items: [{ id: "plan-item-cancelled", status: "PLANNED" }],
    }]);
    mocks.publications.mockResolvedValue([publication({
      id: "cancelled-reschedule",
      content_item_id: "post-cancelled-plan",
      content_version_id: "exact-v3",
      channel: "TELEGRAM",
      status: "CANCELLED",
      scheduled_at: "2026-10-30T09:00:00Z",
      publication_plan_item_id: "plan-item-cancelled",
    })]);

    render(<PublicationsPage />);
    const overdueSection = await screen.findByRole("region", { name: "Просрочены / Требуют решения" });
    expect(within(overdueSection).queryByText("Отменённый плановый пост")).not.toBeInTheDocument();
    const cancelledSection = screen.getByRole("region", { name: "Отменённые" });
    expect(await within(cancelledSection).findByText("Отменённый плановый пост")).toBeInTheDocument();
  });

  it("offers explicit publish-now only when the provider is enabled", async () => {
    mocks.content.mockResolvedValue([{ id: "post-1", title: "Просроченный пост" }]);
    mocks.publications.mockResolvedValue([publication({
      id: "overdue",
      is_overdue: true,
      lateness_seconds: 3600,
      scheduled_at: "2026-10-09T12:00:00Z",
      publication_plan_item_id: "plan-item-1",
      provider_enabled: true,
    })]);
    mocks.publishNow.mockResolvedValue(publication({
      id: "overdue",
      status: "PUBLISHING",
      is_overdue: false,
      scheduled_at: "2026-10-09T12:00:00Z",
      publication_plan_item_id: "plan-item-1",
      provider_enabled: true,
    }));
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Опубликовать сейчас" }));
    await waitFor(() => expect(mocks.publishNow).toHaveBeenCalledWith("overdue"));
  });

  it("reschedules and cancels an upcoming unbound publication directly", async () => {
    mocks.content.mockResolvedValue([{ id: "post-1", title: "Предстоящий пост" }]);
    mocks.publications.mockResolvedValue([publication({
      id: "upcoming",
      scheduled_at: "2026-10-25T10:00:00Z",
      publication_plan_item_id: null,
    })]);
    mocks.schedule.mockResolvedValue(publication({
      id: "upcoming",
      scheduled_at: "2026-10-27T10:00:00Z",
      publication_plan_item_id: null,
    }));
    render(<PublicationsPage />);
    const section = await screen.findByRole("region", { name: "Предстоящие" });
    fireEvent.click(await within(section).findByRole("button", { name: "Перенести" }));
    const input = within(section).getByLabelText("Дата и время публикации");
    fireEvent.change(input, { target: { value: "2026-10-27T10:00" } });
    fireEvent.click(within(section).getByRole("button", { name: "Сохранить перенос" }));
    await waitFor(() => expect(mocks.schedule).toHaveBeenCalledWith(
      "upcoming",
      new Date("2026-10-27T10:00").toISOString(),
    ));
    expect(within(section).getByRole("button", { name: "Отменить" })).toBeInTheDocument();
  });

  it("replans a cancelled post once and moves the old record to history", async () => {
    mocks.content.mockResolvedValue([{ id: "post-1", title: "Отменённый пост" }]);
    mocks.publications.mockResolvedValue([publication({ id: "cancelled-old", status: "CANCELLED" })]);
    mocks.replan.mockResolvedValue(publication({ id: "replacement", status: "SCHEDULED", scheduled_at: "2026-10-28T10:00:00Z" }));
    render(<PublicationsPage />);
    const cancelled = await screen.findByRole("region", { name: "Отменённые" });
    fireEvent.click(await within(cancelled).findByRole("button", { name: "Запланировать заново" }));
    fireEvent.change(within(cancelled).getByLabelText("Дата и время публикации"), { target: { value: "2026-10-28T10:00" } });
    fireEvent.click(within(cancelled).getByRole("button", { name: "Сохранить перенос" }));
    await waitFor(() => expect(mocks.replan).toHaveBeenCalledTimes(1));
    expect(mocks.publishNow).not.toHaveBeenCalled();
    expect(await screen.findByText(/Есть новая запланированная или опубликованная запись/)).toBeInTheDocument();
    expect(within(cancelled).queryByRole("button", { name: "Запланировать заново" })).not.toBeInTheDocument();
  });

  it("opens a separate copy of archived material without planning or sending it", async () => {
    mocks.content.mockResolvedValue([{ id: "post-1", title: "Архивный пост", status: "ARCHIVED" }]);
    mocks.publications.mockResolvedValue([publication({ id: "archived-cancelled", status: "CANCELLED" })]);
    mocks.reuse.mockResolvedValue({ id: "new-copy" });
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Создать копию для новой публикации" }));
    await waitFor(() => expect(mocks.router.push).toHaveBeenCalledWith("/content/new-copy#approval"));
    expect(mocks.reuse).toHaveBeenCalledWith("post-1");
    expect(mocks.replan).not.toHaveBeenCalled();
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it("provides retry and reconciliation actions for failed publications", async () => {
    mocks.content.mockResolvedValue([{ id: "post-2", title: "Failed title" }]);
    mocks.publications.mockResolvedValue([publication({
      id: "failed",
      content_item_id: "post-2",
      status: "FAILED",
      scheduled_at: null,
      failure_code: "VK_RECONCILIATION_REQUIRED",
      reconciliation_required: true,
      retry_allowed: true,
    })]);
    render(<PublicationsPage />);
    const section = await screen.findByRole("region", { name: "Ошибки отправки" });
    expect(await within(section).findByRole("button", { name: "Подтвердить публикацию" })).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Подтвердить отсутствие публикации" })).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });

  it("does not imply worker readiness from provider configuration", async () => {
    mocks.calendar.mockResolvedValue([{
      publication_id: "s",
      content_item_id: "p",
      content_version_id: "v",
      title: "Post",
      status: "SCHEDULED",
      channel: "VK",
      scheduled_at: "2026-10-25T12:00:00Z",
      provider_enabled: true,
    }]);
    render(<PublicationsPage />);
    expect(await screen.findByText(/Автоматическая отправка включена/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Опубликовать/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Операционные действия временно недоступны/)).toBeInTheDocument();
  });

  it("offers recovery routes in empty and error states, retrying without reloading the window", async () => {
    mocks.campaigns.mockRejectedValueOnce(new Error("offline"));
    render(<PublicationsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByRole("link", { name: "Проверить контент и согласования" })).toHaveAttribute("href", "/content");
    expect(mocks.campaigns.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});

describe("partial read failures", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.searchParams = new URLSearchParams();
    mocks.campaigns.mockResolvedValue([{ id: "campaign-1", name: "Запуск", status: "ACTIVE" }]);
    mocks.calendar.mockResolvedValue([{
      publication_id: "s",
      content_item_id: "p",
      content_version_id: "exact-v1",
      title: "Surviving post",
      status: "SCHEDULED",
      channel: "VK",
      scheduled_at: "2026-10-25T12:00:00Z",
    }]);
    mocks.publications.mockResolvedValue([publication({
      id: "s",
      content_item_id: "p",
      content_version_id: "exact-v1",
      scheduled_at: "2026-10-25T12:00:00Z",
    })]);
    mocks.content.mockResolvedValue([{ id: "p", title: "Surviving post" }]);
    mocks.plans.mockResolvedValue([]);
  });

  it.each(["calendar", "publications", "content", "plans"] as const)("keeps rows and warns on %s failure; retry is read-only", async (kind) => {
    mocks[kind].mockRejectedValueOnce(new Error("Failed to fetch"));
    render(<PublicationsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось обновить этот раздел");
    expect(screen.getByText("exact-v1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Surviving post" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Не удалось загрузить публикации" })).not.toBeInTheDocument();
    if (kind !== "calendar") expect(screen.queryByText(/Готовых к планированию постов пока нет/)).not.toBeInTheDocument();
    if (kind === "publications" || kind === "calendar") expect(screen.queryByText("Ошибок отправки нет.")).not.toBeInTheDocument();
    const spy = vi.spyOn(globalThis, "fetch");
    fireEvent.click(screen.getByRole("button", { name: /Повторить загрузку/ }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(mocks[kind]).toHaveBeenCalledTimes(2);
    expect(mocks.campaigns).toHaveBeenCalledTimes(2);
    expect(mocks.content).toHaveBeenLastCalledWith({ campaign_id: "campaign-1", content_type: "SOCIAL_POST", include_archived: "true" });
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("preserves prior rows when publication refresh fails", async () => {
    mocks.plans.mockRejectedValueOnce(new Error("offline"));
    render(<PublicationsPage />);
    expect(await screen.findByText("exact-v1")).toBeInTheDocument();
    mocks.publications.mockRejectedValueOnce(new Error("offline"));
    mocks.calendar.mockRejectedValueOnce(new Error("offline"));
    fireEvent.click(screen.getByRole("button", { name: /Повторить загрузку/ }));
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getByText("exact-v1")).toBeInTheDocument();
  });

  it("does not infer scheduling readiness from incomplete publication and plan reads", async () => {
    mocks.publications.mockRejectedValueOnce(new Error("offline"));
    mocks.plans.mockRejectedValueOnce(new Error("offline"));
    mocks.content.mockResolvedValue([{
      id: "ready",
      title: "Ready",
      content_type: "SOCIAL_POST",
      status: "APPROVED",
      current_version_id: "v",
      approved_version_id: "v",
    }]);
    render(<PublicationsPage />);
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.queryByRole("link", { name: "Открыть пост и запланировать" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Готовых к планированию постов пока нет/)).not.toBeInTheDocument();
  });

  it("keeps another campaign's rows when all reads for one campaign fail", async () => {
    mocks.campaigns.mockResolvedValue([
      { id: "campaign-1", name: "Запуск", status: "ACTIVE" },
      { id: "campaign-2", name: "Second", status: "ACTIVE" },
    ]);
    for (const kind of ["calendar", "publications", "content", "plans"] as const) {
      const value = await mocks[kind]();
      mocks[kind].mockClear();
      mocks[kind].mockImplementation((arg) => (
        (typeof arg === "string" ? arg : arg.campaign_id) === "campaign-2"
          ? Promise.reject(new Error("offline"))
          : Promise.resolve(value)
      ));
    }
    render(<PublicationsPage />);
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(4));
    expect(screen.getByText("exact-v1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Surviving post" })).toBeInTheDocument();
  });

  it("prefers fresh calendar versions over a failed list refresh cache", async () => {
    mocks.plans.mockRejectedValueOnce(new Error("offline"));
    render(<PublicationsPage />);
    expect(await screen.findByText("exact-v1")).toBeInTheDocument();
    mocks.publications.mockRejectedValueOnce(new Error("offline"));
    mocks.calendar.mockResolvedValue([{
      publication_id: "s",
      content_item_id: "p",
      content_version_id: "fresh-v2",
      title: "Surviving post",
      status: "SCHEDULED",
      channel: "VK",
      scheduled_at: "2026-10-25T12:00:00Z",
    }]);
    fireEvent.click(screen.getByRole("button", { name: /Повторить загрузку/ }));
    expect(await screen.findByText("fresh-v2")).toBeInTheDocument();
    expect(screen.queryByText("exact-v1")).not.toBeInTheDocument();
  });
});
