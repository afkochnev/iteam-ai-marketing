import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import TaskDetailsPage from "../app/tasks/[id]/page";
import NewTaskPage from "../app/tasks/new/page";
import TasksPage from "../app/tasks/page";

const mocks = vi.hoisted(() => ({ push: vi.fn(), list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), start: vi.fn(), complete: vi.fn(), cancel: vi.fn(), addDependency: vi.fn(), removeDependency: vi.fn(), campaignList: vi.fn(), agentList: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push, back: vi.fn() }), useParams: () => ({ id: "task-1" }) }));
vi.mock("@/lib/api", async (importOriginal) => { const actual = await importOriginal<typeof import("@/lib/api")>(); return { ...actual, tasksApi: { list: mocks.list, get: mocks.get, create: mocks.create, update: mocks.update, start: mocks.start, complete: mocks.complete, cancel: mocks.cancel, addDependency: mocks.addDependency, removeDependency: mocks.removeDependency }, campaignsApi: { ...actual.campaignsApi, list: mocks.campaignList }, agentsApi: { ...actual.agentsApi, list: mocks.agentList } }; });

const task = { id: "task-1", campaign_id: "campaign-1", campaign: { id: "campaign-1", name: "Кампания" }, parent_task_id: null, parent_task: null, task_type: "MANUAL" as const, title: "Первая задача", description: "Описание", assigned_agent_id: null, assigned_agent: null, priority: "NORMAL" as const, status: "READY" as const, input_data: {}, output_data: {}, requires_approval: false, error_message: null, retry_count: 0, deadline: null, started_at: null, completed_at: null, dependencies: [], dependents: [], created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z" };

describe("Tasks UI", () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.list.mockResolvedValue([task]); mocks.get.mockResolvedValue(task); mocks.campaignList.mockResolvedValue([{ id: "campaign-1", name: "Кампания" }]); mocks.agentList.mockResolvedValue([]); });

  it("renders list, filters and empty state", async () => { const view = render(<TasksPage />); expect(await screen.findByText("Первая задача")).toBeInTheDocument(); fireEvent.change(screen.getByLabelText("Статус"), { target: { value: "READY" } }); await waitFor(() => expect(mocks.list).toHaveBeenCalledWith(expect.objectContaining({ status: "READY" }))); view.unmount(); mocks.list.mockResolvedValue([]); render(<TasksPage />); expect(await screen.findByText("Задач пока нет")).toBeInTheDocument(); });

  it("creates task and redirects", async () => { mocks.create.mockResolvedValue(task); render(<NewTaskPage />); await screen.findByText("Кампания"); fireEvent.change(screen.getByLabelText("Кампания *"), { target: { value: "campaign-1" } }); fireEvent.change(screen.getByLabelText("Название *"), { target: { value: "Первая задача" } }); fireEvent.click(screen.getByRole("button", { name: "Создать задачу" })); await waitFor(() => expect(mocks.create).toHaveBeenCalled()); expect(mocks.push).toHaveBeenCalledWith("/tasks/task-1"); });

  it("shows dependencies and READY action", async () => { mocks.get.mockResolvedValue({ ...task, dependencies: [{ id: "upstream", title: "Источник", status: "COMPLETED" }] }); mocks.start.mockResolvedValue({ ...task, status: "IN_PROGRESS" }); render(<TaskDetailsPage />); expect(await screen.findByText("Источник")).toBeInTheDocument(); fireEvent.click(screen.getByRole("button", { name: "Начать" })); await waitFor(() => expect(mocks.start).toHaveBeenCalledWith("task-1")); });

  it("does not show start for BLOCKED and completes IN_PROGRESS", async () => { mocks.get.mockResolvedValue({ ...task, status: "BLOCKED" }); const blockedView = render(<TaskDetailsPage />); expect(await screen.findByText("Заблокирована")).toBeInTheDocument(); expect(screen.queryByRole("button", { name: "Начать" })).not.toBeInTheDocument(); blockedView.unmount(); mocks.get.mockResolvedValue({ ...task, status: "IN_PROGRESS" }); mocks.complete.mockResolvedValue({ ...task, status: "COMPLETED" }); render(<TaskDetailsPage />); await screen.findByRole("button", { name: "Завершить" }); fireEvent.click(screen.getByRole("button", { name: "Завершить" })); await waitFor(() => expect(mocks.complete).toHaveBeenCalled()); });

  it("shows API errors", async () => { mocks.list.mockRejectedValue(new Error("Ошибка задач")); render(<TasksPage />); expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка задач"); });
});
