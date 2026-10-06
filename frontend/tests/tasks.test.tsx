import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import TaskDetailsPage from "../app/tasks/[id]/page";
import NewTaskPage from "../app/tasks/new/page";
import TasksPage from "../app/tasks/page";

const mocks = vi.hoisted(() => ({ push: vi.fn(), list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), start: vi.fn(), complete: vi.fn(), cancel: vi.fn(), run: vi.fn(), retry: vi.fn(), runList: vi.fn(), packList: vi.fn(), analyses: vi.fn(), addDependency: vi.fn(), removeDependency: vi.fn(), campaignList: vi.fn(), agentList: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push, back: vi.fn() }), useParams: () => ({ id: "task-1" }) }));
vi.mock("@/lib/api", async (importOriginal) => { const actual = await importOriginal<typeof import("@/lib/api")>(); return { ...actual, tasksApi: { list: mocks.list, get: mocks.get, create: mocks.create, update: mocks.update, start: mocks.start, complete: mocks.complete, cancel: mocks.cancel, run: mocks.run, retry: mocks.retry, addDependency: mocks.addDependency, removeDependency: mocks.removeDependency }, agentRunsApi: { list: mocks.runList, get: vi.fn() }, knowledgePacksApi: { list: mocks.packList, get: vi.fn() }, feedbackApi: { ...actual.feedbackApi, analyses: mocks.analyses }, campaignsApi: { ...actual.campaignsApi, list: mocks.campaignList }, agentsApi: { ...actual.agentsApi, list: mocks.agentList } }; });

const task = { id: "task-1", campaign_id: "campaign-1", campaign: { id: "campaign-1", name: "Кампания" }, parent_task_id: null, parent_task: null, task_type: "MANUAL" as const, title: "Первая задача", description: "Описание", assigned_agent_id: null, assigned_agent: null, priority: "NORMAL" as const, status: "READY" as const, classification: "actionable" as const, classification_label: null, input_data: {}, output_data: {}, requires_approval: false, error_message: null, retry_count: 0, deadline: null, started_at: null, completed_at: null, dependencies: [], dependents: [], created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z" };

describe("Tasks UI", () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.list.mockResolvedValue([task]); mocks.get.mockResolvedValue(task); mocks.runList.mockResolvedValue([]); mocks.packList.mockResolvedValue([]); mocks.analyses.mockResolvedValue([]); mocks.campaignList.mockResolvedValue([{ id: "campaign-1", name: "Кампания" }]); mocks.agentList.mockResolvedValue([]); });

  it("renders list, filters locally and shows the empty state", async () => { const view = render(<TasksPage />); expect(await screen.findByText("Первая задача")).toBeInTheDocument(); fireEvent.change(screen.getByLabelText("Статус"), { target: { value: "FAILED" } }); expect(screen.queryByText("Первая задача")).not.toBeInTheDocument(); view.unmount(); mocks.list.mockResolvedValue([]); render(<TasksPage />); expect(await screen.findByText("Задач пока нет")).toBeInTheDocument(); });

  it("explains the task dashboard counts and separates urgent work from history", async () => {
    mocks.list.mockResolvedValue([
      { ...task, id: "failed", title: "Ошибка", status: "FAILED" },
      { ...task, id: "superseded", title: "Старая ошибка", status: "FAILED", classification: "superseded", classification_label: "Заменена успешным выполнением" },
      { ...task, id: "ready", title: "Следующий шаг", status: "READY" },
      { ...task, id: "done", title: "Готово", status: "COMPLETED" },
    ]);
    render(<TasksPage />);
    expect(await screen.findByText("Ошибка")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Требуют внимания/ })).toHaveAttribute("title", "Ошибки и задачи, ожидающие зависимость");
    expect(screen.getByRole("link", { name: /Готовы к выполнению/ })).toHaveAttribute("title", "Можно запустить следующим шагом");
    expect(screen.getByText("Показать завершённые задачи и историю · 2")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Показать завершённые задачи и историю · 2"));
    expect(await screen.findByText("Старая ошибка")).toBeInTheDocument();
    expect(screen.getByText("Заменена успешным выполнением")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Требуют внимания/ })).toHaveTextContent("1");
  });

  it("opens task history on direct hash navigation after async loading", async () => {
    window.history.replaceState(null, "", "/tasks#tasks-history");
    mocks.list.mockResolvedValue([{ ...task, status: "COMPLETED" }]);
    const view = render(<TasksPage />);
    try {
      await waitFor(() => expect(document.getElementById("tasks-history")).toHaveAttribute("open"));
      expect(document.activeElement?.id).toBe("tasks-history");
    } finally { view.unmount(); window.history.replaceState(null, "", "/"); }
  });

  it("creates task and redirects", async () => { mocks.create.mockResolvedValue(task); render(<NewTaskPage />); await screen.findByText("Кампания"); fireEvent.change(screen.getByLabelText("Кампания *"), { target: { value: "campaign-1" } }); fireEvent.change(screen.getByLabelText("Название *"), { target: { value: "Первая задача" } }); fireEvent.click(screen.getByRole("button", { name: "Создать задачу" })); await waitFor(() => expect(mocks.create).toHaveBeenCalled()); expect(mocks.push).toHaveBeenCalledWith("/tasks/task-1"); });

  it("shows dependencies and READY action", async () => { mocks.get.mockResolvedValue({ ...task, dependencies: [{ id: "upstream", title: "Источник", status: "COMPLETED" }] }); mocks.start.mockResolvedValue({ ...task, status: "IN_PROGRESS" }); render(<TaskDetailsPage />); expect(await screen.findByText("Источник")).toBeInTheDocument(); fireEvent.click(screen.getByRole("button", { name: "Начать" })); await waitFor(() => expect(mocks.start).toHaveBeenCalledWith("task-1")); });

  it("does not show start for BLOCKED and completes IN_PROGRESS", async () => { mocks.get.mockResolvedValue({ ...task, status: "BLOCKED" }); const blockedView = render(<TaskDetailsPage />); expect(await screen.findByText("Заблокирована")).toBeInTheDocument(); expect(screen.queryByRole("button", { name: "Начать" })).not.toBeInTheDocument(); blockedView.unmount(); mocks.get.mockResolvedValue({ ...task, status: "IN_PROGRESS" }); mocks.complete.mockResolvedValue({ ...task, status: "COMPLETED" }); render(<TaskDetailsPage />); await screen.findByRole("button", { name: "Завершить" }); fireEvent.click(screen.getByRole("button", { name: "Завершить" })); await waitFor(() => expect(mocks.complete).toHaveBeenCalled()); });

  it("shows API errors", async () => { mocks.list.mockRejectedValue(new Error("Ошибка задач")); render(<TasksPage />); expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка задач"); });

  it("keeps task details visible when auxiliary history requests fail", async () => {
    mocks.runList.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(<TaskDetailsPage />);
    expect(await screen.findByRole("heading", { name: "Первая задача" })).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("Задача открыта, но часть связанных сведений не загрузилась");
  });

  it("offers a retry when the task detail request itself fails", async () => {
    mocks.get.mockRejectedValueOnce(new TypeError("Failed to fetch")).mockResolvedValue(task);
    render(<TaskDetailsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось связаться с API");
    fireEvent.click(screen.getByRole("button", { name: "Повторить загрузку" }));
    expect(await screen.findByRole("heading", { name: "Первая задача" })).toBeInTheDocument();
  });

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

  it("shows autonomous Writer waiting state without a manual launch", async () => {
    const assigned = { id: "agent-1", name: "Writer", slug: "writer" };
    mocks.get.mockResolvedValue({ ...task, task_type: "WRITE_ARTICLE", assigned_agent: assigned });
    const unsupported = render(<TaskDetailsPage />);
    await screen.findByText("Написание статьи");
    expect(screen.queryByRole("button", { name: "Запустить AI" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("ожидает автоматического запуска");
    unsupported.unmount();
    mocks.get.mockResolvedValue({ ...task, status: "BLOCKED", assigned_agent: assigned });
    render(<TaskDetailsPage />);
    await screen.findByText("Заблокирована");
    expect(screen.queryByRole("button", { name: "Запустить AI" })).not.toBeInTheDocument();
  });

  it("runs campaign planning explicitly for Marketing Director only", async () => {
    const director = { id: "director-1", name: "Marketing Director", slug: "marketing_director" };
    mocks.get.mockResolvedValue({ ...task, task_type: "CAMPAIGN_PLANNING", assigned_agent: director });
    mocks.run.mockResolvedValue({});
    render(<TaskDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Запустить AI" }));
    await waitFor(() => expect(mocks.run).toHaveBeenCalledTimes(1));
    expect(mocks.run).toHaveBeenCalledWith("task-1");
    expect(await screen.findByText("AI-запуск поставлен в очередь.")).toBeInTheDocument();
  });

  it("hides campaign planning run for wrong agent and completed task", async () => {
    mocks.get.mockResolvedValue({
      ...task,
      task_type: "CAMPAIGN_PLANNING",
      assigned_agent: { id: "writer-1", name: "Writer", slug: "writer" },
    });
    const wrongAgent = render(<TaskDetailsPage />);
    await screen.findByText("Планирование кампании");
    expect(screen.queryByRole("button", { name: "Запустить AI" })).not.toBeInTheDocument();
    wrongAgent.unmount();
    mocks.get.mockResolvedValue({ ...task, task_type: "CAMPAIGN_PLANNING", status: "COMPLETED", assigned_agent: { id: "director-1", name: "Marketing Director", slug: "marketing_director" } });
    render(<TaskDetailsPage />);
    expect(await screen.findAllByText("Завершена")).not.toHaveLength(0);
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

  it("observes autonomous Knowledge Keeper and renders verified KnowledgePack", async () => {
    const keeper = { id: "keeper-1", name: "Knowledge Keeper", slug: "knowledge_keeper" };
    mocks.get.mockResolvedValue({ ...task, task_type: "KNOWLEDGE_RESEARCH", assigned_agent_id: keeper.id, assigned_agent: keeper });
    mocks.packList.mockResolvedValue([{ id: "pack-1", campaign_id: task.campaign_id, task_id: task.id, agent_run_id: "run-1", created_by_agent_id: keeper.id, strategy_version: 1, status: "READY", research_query: "управленческий ритм", summary: "Материалы найдены", gaps: [], metadata: {}, created_at: task.created_at, items: [{ knowledge_item_id: "knowledge-1", source_title: "Ручные загрузки", filename: "management.md", file_id: "file-1", excerpt: "Проверенный фрагмент", relevance_score: 0.91, selection_reason: "Раскрывает тему", position: 1, result_key: "a".repeat(64) }] }]);
    mocks.run.mockResolvedValue({});
    render(<TaskDetailsPage />);
    expect(await screen.findByText("Задача создана и ожидает автоматического запуска")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Запустить AI" })).not.toBeInTheDocument();
    expect(mocks.run).not.toHaveBeenCalled();
    expect(await screen.findByText("Пакет знаний")).toBeInTheDocument();
    expect(screen.getByText(/management\.md/)).toBeInTheDocument();
    expect(screen.getByText(/0.910/)).toBeInTheDocument();
    expect(screen.getByText("Проверенный фрагмент")).toBeInTheDocument();
  });

  it.each(["KNOWLEDGE_RESEARCH", "WRITE_ARTICLE", "CREATE_SOCIAL_POSTS", "CONTENT_REVISION"])("does not manually run READY AUTO task %s", async (taskType) => {
    mocks.get.mockResolvedValue({ ...task, task_type: taskType, assigned_agent: { id: "agent", name: "Agent", slug: "smm_manager" } });
    render(<TaskDetailsPage />);
    expect(await screen.findByText("Задача создана и ожидает автоматического запуска")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Запустить AI" })).not.toBeInTheDocument();
    expect(mocks.run).not.toHaveBeenCalled();
  });
  it("polls a READY AUTO task before the scheduler has created its first run", async () => {
    vi.useFakeTimers();
    try {
      mocks.get.mockResolvedValue({ ...task, task_type: "CONTENT_REVISION" }); mocks.runList.mockResolvedValue([]);
      render(<TaskDetailsPage />);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(screen.getByRole("status")).toHaveTextContent("ожидает автоматического запуска");
      mocks.get.mockResolvedValue({ ...task, task_type: "CONTENT_REVISION", status: "IN_PROGRESS" });
      mocks.runList.mockResolvedValue([{ id: "run-new", task_id: task.id, status: "RUNNING", created_at: task.created_at }]);
      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      expect(screen.getByText("Выполняется")).toBeInTheDocument();
      expect(mocks.run).not.toHaveBeenCalled();
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it("renders insufficient gaps and research retry", async () => {
    const keeper = { id: "keeper-1", name: "Knowledge Keeper", slug: "knowledge_keeper" };
    mocks.get.mockResolvedValue({ ...task, task_type: "KNOWLEDGE_RESEARCH", status: "FAILED", error_message: "INSUFFICIENT_KNOWLEDGE", assigned_agent_id: keeper.id, assigned_agent: keeper });
    mocks.packList.mockResolvedValue([{ id: "pack-1", campaign_id: task.campaign_id, task_id: task.id, agent_run_id: "run-1", created_by_agent_id: keeper.id, strategy_version: 1, status: "INSUFFICIENT", research_query: "масштаб компаний", summary: "Недостаточно данных", gaps: ["Нет материалов о компаниях данного масштаба"], metadata: {}, created_at: task.created_at, items: [] }]);
    mocks.retry.mockResolvedValue({});
    render(<TaskDetailsPage />);
    expect(await screen.findByText("Недостаточно материалов в базе знаний")).toBeInTheDocument();
    expect(screen.getByText("Нет материалов о компаниях данного масштаба")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Повторить исследование" }));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith("task-1"));
  });
});
