import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AutonomousPostProgress } from "../components/autonomous-post-progress";
const mocks = vi.hoisted(() => ({ get: vi.fn(), list: vi.fn() }));
vi.mock("@/lib/api", () => ({ tasksApi: { get: mocks.get }, agentRunsApi: { list: mocks.list } }));
describe("autonomous post observation", () => {
  beforeEach(() => { vi.useFakeTimers(); mocks.get.mockReset(); mocks.list.mockReset(); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });
  async function tick(ms = 3000) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
  it("discovers asynchronous runs through READY, QUEUED, RUNNING, COMPLETED without requiring a runId", async () => {
    const done = vi.fn();
    mocks.get.mockResolvedValue({ status: "READY", output_data: {} }); mocks.list.mockResolvedValue([]);
    render(<AutonomousPostProgress taskId="task-1" onComplete={done} />);
    await tick(1); expect(screen.getByRole("status")).toHaveTextContent("ожидает автоматического запуска");
    mocks.list.mockResolvedValue([{ id: "run-1", status: "QUEUED", created_at: "2026-10-05T12:00:00Z" }]);
    await tick(); expect(screen.getByRole("status")).toHaveTextContent("в очереди");
    mocks.get.mockResolvedValue({ status: "IN_PROGRESS", output_data: {} }); mocks.list.mockResolvedValue([{ id: "run-1", status: "RUNNING", created_at: "2026-10-05T12:00:00Z" }]);
    await tick(); expect(screen.getByRole("status")).toHaveTextContent("Создаётся пост");
    mocks.get.mockResolvedValue({ status: "COMPLETED", output_data: { content_item_id: "post-1" } }); mocks.list.mockResolvedValue([{ id: "run-1", status: "COMPLETED", created_at: "2026-10-05T12:00:00Z" }]);
    await tick(); expect(screen.getByRole("status")).toHaveTextContent("Пост создан. Проверьте материал."); expect(screen.getByRole("link", { name: "Проверить пост" })).toHaveAttribute("href", "/content/post-1"); expect(done).toHaveBeenCalledTimes(1);
    const calls = mocks.list.mock.calls.length; await tick(6000); expect(mocks.list).toHaveBeenCalledTimes(calls);
  });
  it("survives scheduler latency and shows FAILED with a task recovery route", async () => {
    mocks.get.mockResolvedValue({ status: "READY", output_data: {} }); mocks.list.mockResolvedValue([]);
    render(<AutonomousPostProgress taskId="existing-task" onComplete={vi.fn()} />);
    await tick(9000); expect(mocks.list).toHaveBeenCalledWith({ task_id: "existing-task" });
    mocks.get.mockResolvedValue({ status: "FAILED", output_data: {} }); mocks.list.mockResolvedValue([{ id: "run-1", status: "FAILED", created_at: "2026-10-05T12:00:00Z" }]);
    await tick(); expect(screen.getByRole("status")).toHaveTextContent("остановилось"); expect(screen.getByRole("link", { name: "Открыть задачу" })).toHaveAttribute("href", "/tasks/existing-task");
  });
  it("recovers from a transient read error without creating or running anything", async () => {
    mocks.get.mockRejectedValue(new Error("offline")); mocks.list.mockResolvedValue([]);
    render(<AutonomousPostProgress taskId="task-1" onComplete={vi.fn()} />); await tick(1);
    expect(screen.getByRole("alert")).toHaveTextContent("Повторяем проверку");
    mocks.get.mockResolvedValue({ status: "READY", output_data: {} }); await tick(); expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
