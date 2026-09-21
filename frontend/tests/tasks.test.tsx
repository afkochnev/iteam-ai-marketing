import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import TaskDetailsPage from "../app/tasks/[id]/page";
import NewTaskPage from "../app/tasks/new/page";
import TasksPage from "../app/tasks/page";

const mocks = vi.hoisted(() => ({ push: vi.fn(), list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), start: vi.fn(), complete: vi.fn(), cancel: vi.fn(), run: vi.fn(), retry: vi.fn(), runList: vi.fn(), addDependency: vi.fn(), removeDependency: vi.fn(), campaignList: vi.fn(), agentList: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push, back: vi.fn() }), useParams: () => ({ id: "task-1" }) }));
vi.mock("@/lib/api", async (importOriginal) => { const actual = await importOriginal<typeof import("@/lib/api")>(); return { ...actual, tasksApi: { list: mocks.list, get: mocks.get, create: mocks.create, update: mocks.update, start: mocks.start, complete: mocks.complete, cancel: mocks.cancel, run: mocks.run, retry: mocks.retry, addDependency: mocks.addDependency, removeDependency: mocks.removeDependency }, agentRunsApi: { list: mocks.runList, get: vi.fn() }, campaignsApi: { ...actual.campaignsApi, list: mocks.campaignList }, agentsApi: { ...actual.agentsApi, list: mocks.agentList } }; });

const task = { id: "task-1", campaign_id: "campaign-1", campaign: { id: "campaign-1", name: "Кампания" }, parent_task_id: null, parent_task: null, task_type: "MANUAL" as const, title: "Первая задача", description: "Описание", assigned_agent_id: null, assigned_agent: null, priority: "NORMAL" as const, status: "READY" as const, input_data: {}, output_data: {}, requires_approval: false, error_message: null, retry_count: 0, deadline: null, started_at: null, completed_at: null, dependencies: [], dependents: [], created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z" };

describe("Tasks UI", () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.list.mockResolvedValue([task]); mocks.get.mockResolvedValue(task); mocks.runList.mockResolvedValue([]); mocks.campaignList.mockResolvedValue([{ id: "campaign-1", name: "Кампания" }]); mocks.agentList.mockResolvedValue([]); });

  it("renders list, filters and empty state", async () => { const view = render(<TasksPage />); expect(await screen.findByText("Первая задача")).toBeInTheDocument(); fireEvent.change(screen.getByLabelText("Статус"), { target: { value: "READY" } }); await waitFor(() => expect(mocks.list).toHaveBeenCalledWith(expect.objectContaining({ status: "READY" }))); view.unmount(); mocks.list.mockResolvedValue([]); render(<TasksPage />); expect(await screen.findByText("Задач пока нет")).toBeInTheDocument(); });

  it("creates task and redirects", async () => { mocks.create.mockResolvedValue(task); render(<NewTaskPage />); await screen.findByText("Кампания"); fireEvent.change(screen.getByLabelText("Кампания *"), { target: { value: "campaign-1" } }); fireEvent.change(screen.getByLabelText("Название *"), { target: { value: "Первая задача" } }); fireEvent.click(screen.getByRole("button", { name: "Создать задачу" })); await waitFor(() => expect(mocks.create).toHaveBeenCalled()); expect(mocks.push).toHaveBeenCalledWith("/tasks/task-1"); });

  it("shows dependencies and READY action", async () => { mocks.get.mockResolvedValue({ ...task, dependencies: [{ id: "upstream", title: "Источник", status: "COMPLETED" }] }); mocks.start.mockResolvedValue({ ...task, status: "IN_PROGRESS" }); render(<TaskDetailsPage />); expect(await screen.findByText("Источник")).toBeInTheDocument(); fireEvent.click(screen.getByRole("button", { name: "Начать" })); await waitFor(() => expect(mocks.start).toHaveBeenCalledWith("task-1")); });

  it("does not show start for BLOCKED and completes IN_PROGRESS", async () => { mocks.get.mockResolvedValue({ ...task, status: "BLOCKED" }); const blockedView = render(<TaskDetailsPage />); expect(await screen.findByText("Заблокирована")).toBeInTheDocument(); expect(screen.queryByRole("button", { name: "Начать" })).not.toBeInTheDocument(); blockedView.unmount(); mocks.get.mockResolvedValue({ ...task, status: "IN_PROGRESS" }); mocks.complete.mockResolvedValue({ ...task, status: "COMPLETED" }); render(<TaskDetailsPage />); await screen.findByRole("button", { name: "Завершить" }); fireEvent.click(screen.getByRole("button", { name: "Завершить" })); await waitFor(() => expect(mocks.complete).toHaveBeenCalled()); });

  it("shows API errors", async () => { mocks.list.mockRejectedValue(new Error("Ошибка задач")); render(<TasksPage />); expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка задач"); });

  it("runs a READY MANUAL task and shows queued history", async () => {
    const assigned = { id: "agent-1", name: "Writer", slug: "writer" };
    mocks.get.mockResolvedValue({ ...task, assigned_agent_id: assigned.id, assigned_agent: assigned });
    mocks.run.mockResolvedValue({ id: "run-1", task_id: task.id, agent_id: assigned.id, campaign_id: task.campaign_id, status: "QUEUED", model: "test-model", created_at: task.created_at });
    mocks.runList.mockResolvedValueOnce([]).mockResolvedValue([{ id: "run-1", task_id: task.id, agent_id: assigned.id, campaign_id: task.campaign_id, status: "QUEUED", model: "test-model", created_at: task.created_at, agent: assigned }]);
    render(<TaskDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Запустить AI" }));
    await waitFor(() => expect(mocks.run).toHaveBeenCalledWith("task-1"));
    expect(await screen.findByText("AI-запуск поставлен в очередь.")).toBeInTheDocument();
    expect(await screen.findByText("test-model")).toBeInTheDocument();
  });

  it("hides AI run for unsupported and blocked tasks", async () => {
    const assigned = { id: "agent-1", name: "Writer", slug: "writer" };
    mocks.get.mockResolvedValue({ ...task, task_type: "WRITE_ARTICLE", assigned_agent: assigned });
    const unsupported = render(<TaskDetailsPage />);
    await screen.findByText("Написание статьи");
    expect(screen.queryByRole("button", { name: "Запустить AI" })).not.toBeInTheDocument();
    unsupported.unmount();
    mocks.get.mockResolvedValue({ ...task, status: "BLOCKED", assigned_agent: assigned });
    render(<TaskDetailsPage />);
    await screen.findByText("Заблокирована");
    expect(screen.queryByRole("button", { name: "Запустить AI" })).not.toBeInTheDocument();
  });

  it("shows completed output and failed retry state", async () => {
    mocks.get.mockResolvedValue({ ...task, status: "COMPLETED", output_data: { text: "Готовый ответ" } });
    const completed = render(<TaskDetailsPage />);
    expect(await screen.findByText("Готовый ответ")).toBeInTheDocument();
    completed.unmount();
    mocks.get.mockResolvedValue({ ...task, status: "FAILED", error_message: "Ошибка" });
    mocks.runList.mockResolvedValue([{ id: "run-1", task_id: task.id, agent_id: "agent-1", campaign_id: task.campaign_id, status: "FAILED", model: "test-model", created_at: task.created_at, error_message: "Безопасная ошибка" }]);
    mocks.retry.mockResolvedValue({});
    render(<TaskDetailsPage />);
    expect(await screen.findByText(/Безопасная ошибка/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith("task-1"));
  });
});
