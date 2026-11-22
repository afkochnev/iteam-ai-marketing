import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import PublicationsPage from "../app/publications/page";
import { PublicationScheduleEditor } from "../components/publication-schedule-editor";
import type { DistributionCandidate, DistributionPublication, DistributionWorkspace } from "../lib/api";

const RealDate = Date;
const FIXED_TEST_TIME = new Date("2026-10-21T12:00:00Z");
const campaign = { id: "campaign-1", name: "Запуск", status: "ACTIVE" as const };

const mocks = vi.hoisted(() => ({
  user: { role: "ADMIN" } as { role: string } | null,
  router: { replace: vi.fn(), push: vi.fn() },
  searchParams: new URLSearchParams(),
  workspace: vi.fn(),
  campaigns: vi.fn(), calendar: vi.fn(), publications: vi.fn(), content: vi.fn(), plans: vi.fn(),
  reuse: vi.fn(), approve: vi.fn(), schedule: vi.fn(), replan: vi.fn(), cancel: vi.fn(),
  cancelPlanContent: vi.fn(), reschedulePlanContent: vi.fn(), publishNow: vi.fn(), retry: vi.fn(),
  reconcilePublished: vi.fn(), reconcileNotPublished: vi.fn(),
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
    distributionWorkspaceApi: { get: mocks.workspace },
    campaignsApi: { ...actual.campaignsApi, list: mocks.campaigns },
    contentApi: { ...actual.contentApi, list: mocks.content, reuse: mocks.reuse },
    publicationPlansApi: { ...actual.publicationPlansApi, list: mocks.plans },
    publicationsApi: {
      ...actual.publicationsApi,
      calendar: mocks.calendar, listCampaign: mocks.publications,
      approve: mocks.approve, schedule: mocks.schedule, replan: mocks.replan,
      cancel: mocks.cancel, cancelPlanContent: mocks.cancelPlanContent,
      reschedulePlanContent: mocks.reschedulePlanContent, publishNow: mocks.publishNow,
      retry: mocks.retry, reconcilePublished: mocks.reconcilePublished,
      reconcileNotPublished: mocks.reconcileNotPublished,
    },
  };
});

function publication(overrides: Partial<DistributionPublication> = {}): DistributionPublication {
  return {
    kind: "publication", publication_id: "publication-1", content_item_id: "post-1",
    content_version_id: "exact-v1", title: "Предстоящий пост", campaign, channel: "VK",
    status: "SCHEDULED", scheduled_at: "2026-10-25T10:00:00Z", published_at: null,
    external_url: null, publication_plan_item_id: null, content_status: "APPROVED",
    content_source_archived: false, provider_enabled: false, failure_code: null,
    failure_message: null, is_overdue: false, lateness_seconds: 0,
    sections: ["upcoming"], has_replacement: false, allowed_actions: ["schedule", "cancel"],
    ...overrides,
  };
}

function candidate(overrides: Partial<DistributionCandidate> = {}): DistributionCandidate {
  return {
    kind: "candidate", id: "ready-1", title: "Готовый пост", campaign,
    channel: "VK", plan_channel: "VK", plan_scheduled_at: "2026-10-26T10:00:00Z",
    approved_version_id: "approved-v3", approved_version_number: 3,
    state: "ready", allowed_actions: [], ...overrides,
  };
}

function workspace(overrides: Partial<DistributionWorkspace> = {}): DistributionWorkspace {
  return {
    as_of: "2026-10-21T12:00:00Z", campaign: null, items: [],
    pagination: { limit: 50, next_cursor: null, has_more: false },
    counts: { ready: 0, needs_plan: 0, overdue: 0, active: 0, upcoming: 0, attention: 0, cancelled: 0, cancelled_history: 0, published: 0 },
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.user = { role: "ADMIN" };
  mocks.searchParams = new URLSearchParams();
  mocks.workspace.mockResolvedValue(workspace());
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("distribution workspace", () => {
  it("uses one server snapshot, exact publication versions and server page counts", async () => {
    mocks.workspace.mockResolvedValue(workspace({
      items: [
        publication({ title: "Ближайший пост" }),
        publication({ publication_id: "done", content_item_id: "post-2", title: "Опубликованный пост", status: "PUBLISHED", sections: ["published"], allowed_actions: [] }),
      ],
      counts: { ...workspace().counts, upcoming: 1, published: 1 },
    }));
    render(<PublicationsPage />);
    expect(await screen.findByRole("link", { name: "Ближайший пост" })).toHaveAttribute("href", "/content/post-1");
    expect(screen.getByText("Опубликованные на странице · 1")).toBeInTheDocument();
    expect(screen.getAllByText("exact-v1")[0]).toBeInTheDocument();
    expect(screen.getAllByText("Запуск")[0]).toHaveAttribute("href", "/campaigns/campaign-1");
    expect(screen.getByText(/Снимок на/)).toBeInTheDocument();
    expect(mocks.workspace).toHaveBeenCalledExactlyOnceWith({ campaign_id: undefined, limit: 50, cursor: undefined });
    for (const legacyRead of [mocks.campaigns, mocks.calendar, mocks.publications, mocks.content, mocks.plans]) {
      expect(legacyRead).not.toHaveBeenCalled();
    }
  });

  it("passes the campaign filter and uses the server campaign reference", async () => {
    mocks.searchParams = new URLSearchParams("campaign_id=campaign-2");
    mocks.workspace.mockResolvedValue(workspace({ campaign: { ...campaign, id: "campaign-2", name: "Вторая" } }));
    render(<PublicationsPage />);
    expect(await screen.findByText(/Показана кампания «Вторая»/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "← Вторая" })).toHaveAttribute("href", "/campaigns/campaign-2");
    expect(screen.getByRole("link", { name: "Показать все кампании" })).toHaveAttribute("href", "/publications");
    expect(mocks.workspace).toHaveBeenCalledWith({ campaign_id: "campaign-2", limit: 50, cursor: undefined });
  });

  it("uses server section membership and actions instead of reconstructing status or provider rules", async () => {
    mocks.workspace.mockResolvedValue(workspace({
      items: [publication({
        title: "Решение сервера", status: "DRAFT", provider_enabled: false,
        sections: ["attention"], allowed_actions: ["reconcile_published"],
        failure_code: null, is_overdue: true,
      })],
    }));
    render(<PublicationsPage />);
    const section = await screen.findByRole("region", { name: "Ошибки отправки" });
    expect(await within(section).findByText("Решение сервера")).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Подтвердить публикацию" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Согласовать публикацию" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Опубликовать сейчас" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Предстоящие" })).queryByText("Решение сервера")).not.toBeInTheDocument();
  });

  it("uses server candidate states even when the browser date would classify them differently", async () => {
    mocks.workspace.mockResolvedValue(workspace({
      items: [
        candidate({ plan_scheduled_at: "2000-01-01T00:00:00Z" }),
        candidate({ id: "needs-plan", title: "Нужен план", state: "needs_plan" }),
        candidate({ id: "overdue", title: "Просроченный слот", state: "overdue", plan_scheduled_at: "2099-01-01T00:00:00Z", allowed_actions: ["cancel_plan"] }),
      ],
    }));
    render(<PublicationsPage />);
    expect(await screen.findByRole("link", { name: "Открыть пост и запланировать" })).toHaveAttribute("href", "/content/ready-1#publication");
    expect(screen.getByText(/утверждена версия v3/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть медиаплан" })).toHaveAttribute("href", "/campaigns/campaign-1/plan");
    expect(within(screen.getByRole("region", { name: "Просрочены / Требуют решения" })).getByText("Просроченный слот")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отменить слот" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Перенести" })).not.toBeInTheDocument();
  });

  it("does not infer any mutation from a raw eligible status when the server withholds actions", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      status: "DRAFT", sections: ["active"], provider_enabled: true, allowed_actions: [],
    })] }));
    render(<PublicationsPage />);
    await screen.findByRole("link", { name: "Предстоящий пост" });
    expect(screen.queryByRole("button", { name: "Согласовать публикацию" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отменить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Изменить канал публикации" })).not.toBeInTheDocument();
  });

  it("offers command-specific tools only for server-authorized rows", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      status: "FAILED", sections: ["attention"], allowed_actions: ["change_channel", "batch_replan"],
    })] }));
    render(<PublicationsPage />);
    expect(await screen.findByRole("button", { name: "Изменить канал публикации" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Предпросмотр перепланирования" })).toBeInTheDocument();
  });

  it("keeps an archived source cancellation-only according to server actions", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      title: "Архивный пост", status: "APPROVED", sections: ["active"],
      content_source_archived: true, allowed_actions: ["cancel"],
    })] }));
    render(<PublicationsPage />);
    expect(await screen.findByText(/Материал или кампания находятся в архиве/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отменить публикацию" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Назначить время" })).not.toBeInTheDocument();
  });

  it("keeps publishing read-only and does not equate provider configuration with worker readiness", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      status: "PUBLISHING", sections: ["active"], provider_enabled: true, allowed_actions: [],
    })] }));
    render(<PublicationsPage />);
    expect(await screen.findByText(/результат ещё не подтверждён/)).toBeInTheDocument();
    expect(screen.getByText(/Проверка готовности сервиса — отдельное действие/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Опубликовать сейчас" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отменить" })).not.toBeInTheDocument();
  });

  it("renders server-selected cancelled cards and replacement history without looking on another page", async () => {
    mocks.workspace.mockResolvedValue(workspace({
      items: [
        publication({ publication_id: "older", status: "CANCELLED", sections: ["cancelled_history"], allowed_actions: [] }),
        publication({ publication_id: "representative", status: "CANCELLED", sections: ["cancelled", "cancelled_history"], allowed_actions: ["replan"] }),
        publication({ publication_id: "replaced", content_item_id: "post-replaced", title: "Есть замена вне страницы", status: "CANCELLED", sections: ["cancelled_history"], has_replacement: true, allowed_actions: [] }),
      ],
      counts: { ...workspace().counts, cancelled: 1, cancelled_history: 3 },
    }));
    render(<PublicationsPage />);
    expect(await screen.findByRole("button", { name: "Запланировать заново" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Запланировать заново" })).toHaveLength(1);
    expect(screen.getByText("История отменённых публикаций на странице · 3")).toBeInTheDocument();
    expect(screen.getByText(/Есть новая запланированная или опубликованная запись/)).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Отменённые" })).queryByText("Есть замена вне страницы")).not.toBeInTheDocument();
  });
});

describe("publication operator commands", () => {
  beforeEach(() => {
    expect(vi.isFakeTimers()).toBe(false);
    expect(Date).toBe(RealDate);
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(FIXED_TEST_TIME);
  });

  it.each([
    ["future", "2026-10-27T09:00", true],
    ["past", "2026-10-20T09:00", false],
  ] as const)("validates a %s fixture against the fixed clock", async (_label, fixture, accepted) => {
    const onSubmit = vi.fn().mockResolvedValue(true);
    const onClose = vi.fn();
    render(<PublicationScheduleEditor editorKey="clock-regression" title="Clock regression" submitLabel="Save" busy={false} onClose={onClose} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText("Дата и время публикации"), { target: { value: fixture } });
    fireEvent.submit(screen.getByRole("form", { name: "Clock regression" }));
    if (accepted) {
      await waitFor(() => expect(onSubmit).toHaveBeenCalledExactlyOnceWith(new Date(fixture).toISOString()));
      expect(onClose).toHaveBeenCalledTimes(1);
    } else {
      expect(screen.getByRole("alert")).toHaveTextContent("Выберите корректные будущие дату и время.");
      expect(onSubmit).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
    }
  });

  it.each(["DRAFT", "WAITING_APPROVAL"] as const)("approves %s and reloads the authoritative workspace rather than merging the command result", async (status) => {
    mocks.workspace.mockResolvedValueOnce(workspace({ items: [publication({
      status, title: "До согласования", sections: ["active"], allowed_actions: ["approve", "cancel"],
    })] })).mockResolvedValue(workspace({ items: [publication({
      status: "APPROVED", title: "Сервер обновил представление", sections: ["active"], allowed_actions: ["schedule"],
    })] }));
    mocks.approve.mockResolvedValue({ id: "untrusted-command-id", status: "PUBLISHED" });
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Согласовать публикацию" }));
    expect(await screen.findByRole("link", { name: "Сервер обновил представление" })).toBeInTheDocument();
    expect(mocks.approve).toHaveBeenCalledExactlyOnceWith("publication-1");
    expect(mocks.workspace).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("До согласования")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Назначить время" })).toBeInTheDocument();
  });

  it.each([
    ["schedule", "Назначить время", "Запланировать"],
    ["replan", "Назначить время", "Сохранить перенос"],
  ] as const)("chooses the %s command from server actions", async (action, openLabel, submitLabel) => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      status: "APPROVED", sections: ["active"], publication_plan_item_id: "not-used-for-routing",
      allowed_actions: [action], scheduled_at: null,
    })] }));
    mocks[action].mockResolvedValue({});
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: openLabel }));
    fireEvent.change(screen.getByLabelText("Дата и время публикации"), { target: { value: "2026-10-27T09:00" } });
    fireEvent.click(screen.getByRole("button", { name: submitLabel }));
    await waitFor(() => expect(mocks[action]).toHaveBeenCalledExactlyOnceWith("publication-1", new Date("2026-10-27T09:00").toISOString()));
    expect(mocks[action === "schedule" ? "replan" : "schedule"]).not.toHaveBeenCalled();
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledTimes(2));
  });

  it("cancels an overdue publication and refreshes its history without provider sending", async () => {
    mocks.workspace.mockResolvedValueOnce(workspace({ items: [publication({
      sections: ["overdue"], is_overdue: true, allowed_actions: ["cancel"],
    })] })).mockResolvedValue(workspace({ items: [publication({
      status: "CANCELLED", sections: ["cancelled_history"], allowed_actions: [],
    })] }));
    mocks.cancel.mockResolvedValue({});
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Отменить" }));
    await waitFor(() => expect(mocks.cancel).toHaveBeenCalledExactlyOnceWith("publication-1"));
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledTimes(2));
    expect(within(screen.getByRole("region", { name: "Просрочены / Требуют решения" })).queryByText("Предстоящий пост")).not.toBeInTheDocument();
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it("preserves the schedule command for an unbound upcoming row when both schedule actions are allowed", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({ allowed_actions: ["schedule", "replan"] })] }));
    mocks.schedule.mockResolvedValue({});
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Перенести" }));
    fireEvent.change(screen.getByLabelText("Дата и время публикации"), { target: { value: "2026-10-27T09:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить перенос" }));
    await waitFor(() => expect(mocks.schedule).toHaveBeenCalledExactlyOnceWith("publication-1", new Date("2026-10-27T09:00").toISOString()));
    expect(mocks.replan).not.toHaveBeenCalled();
  });

  it.each(["active", "upcoming", "overdue"] as const)("renders the explicit server publish-now action in %s without inferring it from provider flags", async (section) => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      sections: [section], provider_enabled: false, allowed_actions: ["publish_now"],
    })] }));
    mocks.publishNow.mockResolvedValue({});
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Опубликовать сейчас" }));
    await waitFor(() => expect(mocks.publishNow).toHaveBeenCalledExactlyOnceWith("publication-1"));
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledTimes(2));
  });

  it("reschedules an approved plan slot before a Publication exists", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [candidate({
      state: "overdue", allowed_actions: ["reschedule_plan"], plan_scheduled_at: "2026-10-20T10:00:00Z",
    })] }));
    mocks.reschedulePlanContent.mockResolvedValue({});
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Перенести" }));
    fireEvent.change(screen.getByLabelText("Дата и время публикации"), { target: { value: "2026-10-27T09:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить перенос" }));
    await waitFor(() => expect(mocks.reschedulePlanContent).toHaveBeenCalledExactlyOnceWith("ready-1", new Date("2026-10-27T09:00").toISOString()));
    expect(mocks.schedule).not.toHaveBeenCalled();
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledTimes(2));
  });

  it("cancels an approved overdue slot without sending", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [candidate({ state: "overdue", allowed_actions: ["cancel_plan"] })] }));
    mocks.cancelPlanContent.mockResolvedValue({});
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Отменить слот" }));
    await waitFor(() => expect(mocks.cancelPlanContent).toHaveBeenCalledExactlyOnceWith("ready-1"));
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledTimes(2));
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it("replans a cancelled post once and accepts the server's replacement and history", async () => {
    mocks.workspace.mockResolvedValueOnce(workspace({ items: [publication({
      status: "CANCELLED", sections: ["cancelled", "cancelled_history"], allowed_actions: ["replan"],
    })] })).mockResolvedValue(workspace({ items: [
      publication({ status: "CANCELLED", sections: ["cancelled_history"], has_replacement: true, allowed_actions: [] }),
      publication({ publication_id: "replacement", title: "Новая отправка", sections: ["upcoming"], allowed_actions: [] }),
    ] }));
    mocks.replan.mockResolvedValue({});
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Запланировать заново" }));
    fireEvent.change(screen.getByLabelText("Дата и время публикации"), { target: { value: "2026-10-27T09:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить перенос" }));
    expect(await screen.findByRole("link", { name: "Новая отправка" })).toBeInTheDocument();
    expect(mocks.replan).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Запланировать заново" })).not.toBeInTheDocument();
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it("creates a separate copy of archived material, refreshes and opens its approval", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      content_status: "ARCHIVED", content_source_archived: true, status: "CANCELLED",
      sections: ["cancelled", "cancelled_history"], allowed_actions: ["reuse"],
    })] }));
    mocks.reuse.mockResolvedValue({ id: "new-copy" });
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Создать копию для новой публикации" }));
    await waitFor(() => expect(mocks.router.push).toHaveBeenCalledWith("/content/new-copy#approval"));
    expect(mocks.reuse).toHaveBeenCalledExactlyOnceWith("post-1");
    expect(mocks.workspace).toHaveBeenCalledTimes(2);
    expect(mocks.replan).not.toHaveBeenCalled();
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it.each([
    ["retry", "Повторить", "retry"],
    ["reconcile_not_published", "Подтвердить отсутствие публикации", "reconcileNotPublished"],
  ] as const)("keeps %s a distinct explicit command", async (action, label, mockName) => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      status: "FAILED", sections: ["attention"], allowed_actions: [action],
      failure_code: "DELIVERY_REQUIRES_DECISION",
    })] }));
    mocks[mockName].mockResolvedValue({});
    vi.spyOn(window, "prompt").mockReturnValue("Проверено оператором");
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: label }));
    await waitFor(() => expect(mocks[mockName]).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledTimes(2));
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it("confirms the observed external publication without resending it", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      status: "FAILED", sections: ["attention"], allowed_actions: ["reconcile_published", "reconcile_not_published"],
    })] }));
    mocks.reconcilePublished.mockResolvedValue({});
    vi.spyOn(window, "prompt").mockReturnValueOnce("external-1").mockReturnValueOnce("https://example.test/post").mockReturnValueOnce("").mockReturnValueOnce("Проверено");
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Подтвердить публикацию" }));
    await waitFor(() => expect(mocks.reconcilePublished).toHaveBeenCalledWith("publication-1", {
      external_id: "external-1", external_url: "https://example.test/post", published_at: undefined, note: "Проверено",
    }));
    expect(mocks.retry).not.toHaveBeenCalled();
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });

  it("shows a command rejection without changing the snapshot or calling another command", async () => {
    mocks.workspace.mockResolvedValue(workspace({ items: [publication({
      status: "DRAFT", sections: ["active"], allowed_actions: ["approve"],
    })] }));
    mocks.approve.mockRejectedValue(new Error("Согласованная версия изменилась"));
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Согласовать публикацию" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Согласованная версия изменилась");
    expect(mocks.workspace).toHaveBeenCalledTimes(1);
    expect(mocks.publishNow).not.toHaveBeenCalled();
  });
});

describe("snapshot failures and pagination", () => {
  it("retries an initial error without claiming zero results", async () => {
    mocks.workspace.mockRejectedValueOnce(new Error("offline")).mockResolvedValue(workspace());
    render(<PublicationsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("offline");
    expect(screen.queryByText("На этой странице нет предстоящих публикаций.")).not.toBeInTheDocument();
    const metrics = screen.getByLabelText("Показатели текущей страницы");
    expect(within(metrics).getAllByText("—")).toHaveLength(7);
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByText("На этой странице нет предстоящих публикаций.")).toBeInTheDocument();
    expect(mocks.workspace).toHaveBeenCalledTimes(2);
  });

  it("keeps a failed refresh as one visibly stale snapshot and disables mutations", async () => {
    mocks.workspace.mockResolvedValueOnce(workspace({ items: [publication()] })).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(workspace({ items: [publication({ title: "Свежий снимок" })] }));
    render(<PublicationsPage />);
    await screen.findByRole("link", { name: "Предстоящий пост" });
    fireEvent.click(screen.getByRole("button", { name: "Обновить публикации" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Показан предыдущий снимок");
    expect(screen.getByRole("link", { name: "Предстоящий пост" })).toBeInTheDocument();
    expect(screen.getByText(/Снимок на.*свежесть не подтверждена/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Перенести" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Отменить" })).toBeDisabled();
    expect(within(screen.getByLabelText("Показатели текущей страницы")).getAllByText("—")).toHaveLength(7);
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByRole("link", { name: "Свежий снимок" })).toBeInTheDocument();
    expect(screen.queryByText(/свежесть не подтверждена/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отменить" })).toBeEnabled();
  });

  it("does not report a successful command as failed when its authoritative refresh fails", async () => {
    mocks.workspace.mockResolvedValueOnce(workspace({ items: [publication({
      status: "DRAFT", sections: ["active"], allowed_actions: ["approve"],
    })] })).mockRejectedValue(new Error("refresh unavailable"));
    mocks.approve.mockResolvedValue({});
    render(<PublicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Согласовать публикацию" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("refresh unavailable");
    expect(screen.getAllByText("Публикация согласована.").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Согласовать публикацию" })).toBeDisabled();
    expect(mocks.approve).toHaveBeenCalledTimes(1);
    expect(mocks.workspace).toHaveBeenCalledTimes(2);
  });

  it("replaces complete pages, sends cursor and reloads the previous page independently", async () => {
    const first = workspace({
      items: [publication({ title: "Первая страница" })],
      pagination: { limit: 50, next_cursor: "cursor-a", has_more: true },
      counts: { ...workspace().counts, upcoming: 1 },
    });
    const second = workspace({
      as_of: "2026-10-21T12:01:00Z", items: [candidate({ title: "Вторая страница" })],
      counts: { ...workspace().counts, ready: 1 },
    });
    mocks.workspace.mockResolvedValueOnce(first).mockResolvedValueOnce(second).mockResolvedValue(first);
    render(<PublicationsPage />);
    await screen.findByRole("link", { name: "Первая страница" });
    expect(screen.getByRole("button", { name: "Предыдущая страница" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Следующая страница" }));
    expect(await screen.findByRole("link", { name: "Вторая страница" })).toBeInTheDocument();
    expect(screen.queryByText("Первая страница")).not.toBeInTheDocument();
    expect(mocks.workspace).toHaveBeenNthCalledWith(2, { campaign_id: undefined, limit: 50, cursor: "cursor-a" });
    expect(screen.getByText("Страница 2")).toBeInTheDocument();
    expect(screen.getByText("На этой странице нет предстоящих публикаций.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Следующая страница" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Предыдущая страница" }));
    expect(await screen.findByRole("link", { name: "Первая страница" })).toBeInTheDocument();
    expect(screen.queryByText("Вторая страница")).not.toBeInTheDocument();
    expect(mocks.workspace).toHaveBeenNthCalledWith(3, { campaign_id: undefined, limit: 50, cursor: undefined });
  });

  it("keeps the current page after failed navigation and retries the requested cursor", async () => {
    mocks.workspace.mockResolvedValueOnce(workspace({
      items: [publication({ title: "Сохранённая страница" })],
      pagination: { limit: 50, next_cursor: "next", has_more: true },
    })).mockRejectedValueOnce(new Error("page unavailable")).mockResolvedValue(workspace({ items: [publication({ title: "Следующий снимок" })] }));
    render(<PublicationsPage />);
    await screen.findByRole("link", { name: "Сохранённая страница" });
    fireEvent.click(screen.getByRole("button", { name: "Следующая страница" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("page unavailable");
    expect(screen.getByText("Страница 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отменить" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByRole("link", { name: "Следующий снимок" })).toBeInTheDocument();
    expect(screen.getByText("Страница 2")).toBeInTheDocument();
    expect(mocks.workspace).toHaveBeenNthCalledWith(3, { campaign_id: undefined, limit: 50, cursor: "next" });
  });

  it("clears the old campaign immediately and ignores its late response", async () => {
    const oldRequest = deferred<DistributionWorkspace>();
    mocks.searchParams = new URLSearchParams("campaign_id=campaign-1");
    mocks.workspace.mockResolvedValueOnce(workspace({ campaign, items: [publication({ title: "Первый контекст" })] }))
      .mockReturnValueOnce(oldRequest.promise)
      .mockResolvedValue(workspace({ campaign: { ...campaign, id: "campaign-2", name: "Другая" }, items: [publication({ title: "Новый контекст", campaign: { ...campaign, id: "campaign-2", name: "Другая" } })] }));
    const view = render(<PublicationsPage />);
    await screen.findByRole("link", { name: "Первый контекст" });
    fireEvent.click(screen.getByRole("button", { name: "Обновить публикации" }));
    await waitFor(() => expect(mocks.workspace).toHaveBeenCalledTimes(2));
    mocks.searchParams = new URLSearchParams("campaign_id=campaign-2");
    view.rerender(<PublicationsPage />);
    expect(screen.queryByText("Первый контекст")).not.toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Новый контекст" })).toBeInTheDocument();
    await act(async () => oldRequest.resolve(workspace({ campaign, items: [publication({ title: "Поздний старый ответ" })] })));
    expect(screen.queryByText("Поздний старый ответ")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Новый контекст" })).toBeInTheDocument();
  });

  it("does not request the workspace before authentication", async () => {
    mocks.user = null;
    render(<PublicationsPage />);
    await waitFor(() => expect(mocks.router.replace).toHaveBeenCalledWith("/login"));
    expect(mocks.workspace).not.toHaveBeenCalled();
  });
});

describe("publication clock isolation", () => {
  it("restores real timers after command tests", () => {
    expect(vi.isFakeTimers()).toBe(false);
    expect(Date).toBe(RealDate);
  });
});
