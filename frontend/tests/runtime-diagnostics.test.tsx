import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RuntimeDiagnostics } from "@/components/runtime-diagnostics";
import type { RuntimeSnapshot, User } from "@/lib/api";

const mocks = vi.hoisted(() => ({ runtime: vi.fn(), user: null as User | null, loading: false }));
vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({ user: mocks.user, loading: mocks.loading }),
}));
vi.mock("@/lib/api", async (original) => {
  const actual = await original<typeof import("@/lib/api")>();
  return { ...actual, systemApi: { ...actual.systemApi, runtime: mocks.runtime } };
});

function admin(): User {
  return { id: "admin-1", email: "admin@example.com", full_name: "Admin", role: "ADMIN", is_active: true, created_at: "2026-01-01T00:00:00Z" };
}

function snapshot(overrides: Partial<RuntimeSnapshot> = {}): RuntimeSnapshot {
  const now = Date.now();
  return {
    sampled_at: new Date(now).toISOString(),
    broker_state: "ok",
    stale_after_seconds: 60,
    queues: [{ name: "ai", waiting_messages: 0 }, { name: "publication", waiting_messages: null }],
    processes: [
      { id: "worker:ai", kind: "worker", role: "ai", state: "recent", last_seen_at: new Date(now - 2000).toISOString(), age_seconds: 2, observed_instances: 1, fresh_instances: 1, queues: ["ai", "ai_control"], build_mismatch: false },
      { id: "worker:publication", kind: "worker", role: "publication", state: "stale", last_seen_at: new Date(now - 120_000).toISOString(), age_seconds: 120, observed_instances: 1, fresh_instances: 0, queues: ["publication"], build_mismatch: true },
      { id: "scheduler:ai", kind: "scheduler", role: "ai", state: "unobserved", last_seen_at: null, age_seconds: null, observed_instances: 0, fresh_instances: 0, queues: [], build_mismatch: null },
      { id: "worker:metrics", kind: "worker", role: "metrics", state: "unknown", last_seen_at: null, age_seconds: null, observed_instances: null, fresh_instances: null, queues: [], build_mismatch: null },
    ],
    ...overrides,
  };
}

function check() {
  fireEvent.click(screen.getByRole("button", { name: "Проверить исполнителей и очереди" }));
}

beforeEach(() => {
  mocks.user = admin();
  mocks.loading = false;
  mocks.runtime.mockReset().mockResolvedValue(snapshot());
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Manual runtime diagnostics", () => {
  it("waits for an administrator's explicit request", () => {
    render(<RuntimeDiagnostics />);
    expect(screen.getByRole("heading", { name: "Исполнители и очереди" })).toBeInTheDocument();
    expect(screen.getByText(/Запустите проверку/)).toBeInTheDocument();
    expect(mocks.runtime).not.toHaveBeenCalled();
  });

  it.each(["MANAGER", null] as const)("hides diagnostics from %s without requesting them", (role) => {
    mocks.user = role ? { ...admin(), role } : null;
    render(<RuntimeDiagnostics />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(mocks.runtime).not.toHaveBeenCalled();
  });

  it("waits for authentication before offering diagnostics", () => {
    mocks.loading = true;
    render(<RuntimeDiagnostics />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(mocks.runtime).not.toHaveBeenCalled();
  });

  it("shows observation states and actual worker queues without inferring readiness", async () => {
    render(<RuntimeDiagnostics />);
    check();
    expect(await screen.findByText("Свежий сигнал")).toBeInTheDocument();
    expect(screen.getByText("Сигнал устарел")).toBeInTheDocument();
    expect(screen.getByText("Сигнал не наблюдался")).toBeInTheDocument();
    expect(screen.getByText("Состояние неизвестно")).toBeInTheDocument();
    expect(screen.getByText("ai, ai_control")).toBeInTheDocument();
    expect(screen.getByText("Сигнал относится к другой сборке.")).toBeInTheDocument();
    const queues = screen.getByRole("table", { name: "Очереди брокера" });
    expect(within(queues).getByRole("row", { name: "ai 0" })).toBeInTheDocument();
    expect(within(queues).getByRole("row", { name: "publication —" })).toBeInTheDocument();
    const processes = screen.getByRole("table", { name: "Сигналы исполнителей и планировщиков" });
    const unknown = within(processes).getByRole("row", { name: /Сбор метрик/ });
    expect(within(unknown).getByText("— / —")).toBeInTheDocument();
    expect(screen.getByText(/Доступ к внешним каналам и возможность отправки требуют отдельной проверки/)).toBeInTheDocument();
    expect(screen.queryByText(/Готов к отправке|не запущен/)).not.toBeInTheDocument();
    expect(mocks.runtime).toHaveBeenCalledTimes(1);
    expect(mocks.runtime.mock.calls[0][0]).toBeInstanceOf(AbortSignal);
  });

  it.each([
    ["partial", "Часть диагностических данных недоступна."],
    ["unavailable", "Брокер недоступен. Состояние очередей и сигналов неизвестно."],
    ["unsupported", "Этот режим брокера не поддерживает диагностику очередей и сигналов."],
    ["disabled", "Диагностика очередей и исполнителей отключена."],
  ] as const)("explains broker state %s and preserves unknown counts", async (brokerState, label) => {
    mocks.runtime.mockResolvedValue(snapshot({ broker_state: brokerState, queues: [{ name: "ai", waiting_messages: null }], processes: [] }));
    render(<RuntimeDiagnostics />);
    check();
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.getByRole("row", { name: "ai —" })).toBeInTheDocument();
    expect(screen.queryByText("Свежий сигнал")).not.toBeInTheDocument();
  });

  it("prevents duplicate requests while a check is pending", async () => {
    let finish!: (value: RuntimeSnapshot) => void;
    mocks.runtime.mockReturnValue(new Promise<RuntimeSnapshot>((resolve) => { finish = resolve; }));
    render(<RuntimeDiagnostics />);
    check();
    const button = screen.getByRole("button", { name: "Проверяем исполнителей и очереди…" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(mocks.runtime).toHaveBeenCalledTimes(1);
    await act(async () => finish(snapshot()));
    expect(screen.getByRole("button", { name: "Проверить исполнителей и очереди" })).toBeEnabled();
  });

  it("shows a safe first-request error without displaying exception details", async () => {
    mocks.runtime.mockRejectedValue(new Error("sensitive infrastructure details"));
    render(<RuntimeDiagnostics />);
    check();
    expect(await screen.findByRole("alert")).toHaveTextContent("Диагностика недоступна. Повторите проверку.");
    expect(screen.queryByText(/sensitive infrastructure/)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("clears previous fresh observations when refresh fails", async () => {
    render(<RuntimeDiagnostics />);
    check();
    await screen.findByText("Свежий сигнал");
    mocks.runtime.mockRejectedValueOnce(new Error("network"));
    check();
    expect(await screen.findByRole("alert")).toHaveTextContent("Диагностика недоступна");
    expect(screen.queryByText("Свежий сигнал")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("expires a snapshot locally without polling and accepts a new manual check", async () => {
    vi.useFakeTimers();
    mocks.runtime.mockResolvedValue(snapshot());
    render(<RuntimeDiagnostics />);
    await act(async () => { check(); });
    expect(screen.getByText("Свежий сигнал")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(screen.getByRole("alert")).toHaveTextContent("Срок проверки истёк");
    expect(screen.queryByText("Свежий сигнал")).not.toBeInTheDocument();
    expect(screen.getByText("Свежесть сигнала не подтверждена")).toBeInTheDocument();
    expect(screen.getByRole("row", { name: "ai —" })).toBeInTheDocument();
    expect(mocks.runtime).toHaveBeenCalledTimes(1);
    mocks.runtime.mockResolvedValueOnce(snapshot());
    await act(async () => { check(); });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Свежий сигнал")).toBeInTheDocument();
    expect(mocks.runtime).toHaveBeenCalledTimes(2);
  });

  it("expires an almost-stale signal after its remaining second without polling", async () => {
    vi.useFakeTimers();
    const data = snapshot();
    data.processes[0].age_seconds = 59;
    data.processes[0].last_seen_at = new Date(Date.now() - 59_000).toISOString();
    mocks.runtime.mockResolvedValue(data);
    render(<RuntimeDiagnostics />);
    await act(async () => { check(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(999); });
    expect(screen.getByText("Свежий сигнал")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByRole("alert")).toHaveTextContent("Срок проверки истёк");
    expect(screen.queryByText("Свежий сигнал")).not.toBeInTheDocument();
    expect(screen.getByRole("row", { name: "ai —" })).toBeInTheDocument();
    expect(mocks.runtime).toHaveBeenCalledTimes(1);
  });

  it.each([null, -1, Infinity, NaN])("expires a recent signal with invalid age %s immediately", async (age) => {
    vi.useFakeTimers();
    const data = snapshot();
    data.processes[0].age_seconds = age;
    mocks.runtime.mockResolvedValue(data);
    render(<RuntimeDiagnostics />);
    await act(async () => { check(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("alert")).toHaveTextContent("Срок проверки истёк");
    expect(screen.queryByText("Свежий сигнал")).not.toBeInTheDocument();
    expect(mocks.runtime).toHaveBeenCalledTimes(1);
  });

  it("aborts an in-flight request when the role changes and ignores its late result", async () => {
    let finish!: (value: RuntimeSnapshot) => void;
    mocks.runtime.mockReturnValueOnce(new Promise<RuntimeSnapshot>((resolve) => { finish = resolve; }));
    const { rerender } = render(<RuntimeDiagnostics />);
    check();
    const signal = mocks.runtime.mock.calls[0][0] as AbortSignal;
    mocks.user = { ...admin(), role: "MANAGER" };
    rerender(<RuntimeDiagnostics />);
    expect(signal.aborted).toBe(true);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    await act(async () => finish(snapshot()));
    mocks.user = admin();
    rerender(<RuntimeDiagnostics />);
    expect(screen.getByText(/Запустите проверку/)).toBeInTheDocument();
    expect(screen.queryByText("Свежий сигнал")).not.toBeInTheDocument();
  });

  it("aborts a pending request on unmount", () => {
    mocks.runtime.mockReturnValue(new Promise<RuntimeSnapshot>(() => {}));
    const { unmount } = render(<RuntimeDiagnostics />);
    check();
    const signal = mocks.runtime.mock.calls[0][0] as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
  });

  it("uses only the authenticated GET runtime endpoint with the caller's signal", async () => {
    const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => snapshot() });
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    await actual.systemApi.runtime(controller.signal);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(expect.stringMatching(/\/system\/runtime$/), expect.objectContaining({ method: "GET", credentials: "include", signal: controller.signal }));
  });
});
