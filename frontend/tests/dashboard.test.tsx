import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import Home from "../app/page";
import { approvalsApi, campaignsApi, systemApi, tasksApi } from "../lib/api";

const replace = vi.fn();
const logout = vi.fn();
let currentUser: { full_name: string; email: string; role: "ADMIN" } | null;
const campaignsList = vi.spyOn(campaignsApi, "list");
const tasksList = vi.spyOn(tasksApi, "list");
const approvalsList = vi.spyOn(approvalsApi, "list");
const systemStatus = vi.spyOn(systemApi, "status");

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({ user: currentUser, loading: false, login: vi.fn(), logout }),
}));

describe("Protected dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentUser = null;
    campaignsList.mockResolvedValue([]);
    tasksList.mockResolvedValue([]);
    approvalsList.mockResolvedValue([]);
    systemStatus.mockResolvedValue({
      tasks: { READY: 0, IN_PROGRESS: 0, FAILED: 0 }, agent_runs: { RUNNING: 0, FAILED: 0 },
      stuck_tasks: 0, pending_approvals: 0, last_activity_at: null,
    });
  });

  it("redirects an unauthenticated visitor", async () => {
    render(<Home />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("shows a useful overview and links to campaigns and attention tasks", async () => {
    currentUser = { full_name: "Admin User", email: "admin@example.com", role: "ADMIN" };
    campaignsList.mockResolvedValue([{
      id: "campaign-1", name: "Кампания запуска", goal: "Вывести новый продукт", product: "Продукт",
      status: "ACTIVE", start_date: "2026-10-01", end_date: "2026-10-31",
      created_at: "2026-09-20T10:00:00Z", updated_at: "2026-09-25T10:00:00Z",
    }]);
    tasksList.mockResolvedValue([{
      id: "task-1", campaign_id: "campaign-1", campaign: { id: "campaign-1", name: "Кампания запуска" },
      task_type: "WRITE_ARTICLE", title: "Проверить проблемную задачу", assigned_agent: null,
      priority: "HIGH", status: "FAILED", classification: "actionable", classification_label: null, deadline: null,
      created_at: "2026-09-25T10:00:00Z", updated_at: "2026-09-26T10:00:00Z",
    }]);
    approvalsList.mockResolvedValue([]);
    systemStatus.mockResolvedValue({
      tasks: { READY: 2, IN_PROGRESS: 1, FAILED: 1 }, agent_runs: { RUNNING: 1, FAILED: 0 },
      stuck_tasks: 0, pending_approvals: 3, last_activity_at: "2026-09-26T10:00:00Z",
    });

    render(<Home />);

    expect(await screen.findByRole("heading", { name: "Маркетинг под контролем" })).toBeInTheDocument();
    expect(await screen.findAllByText("Кампания запуска")).toHaveLength(2);
    expect(await screen.findByText("Проверить проблемную задачу")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Активные кампании/ })).toHaveAttribute("href", "/campaigns?status=ACTIVE");
    expect(screen.getByRole("link", { name: /Требуют внимания/ })).toHaveAttribute("href", "/tasks?status=FAILED");
    expect(screen.getByText("Ожидают согласования")).toBeInTheDocument();
    expect(screen.getByText("3", { selector: "strong" })).toBeInTheDocument();
  });

  it("shows the real user and logs out", async () => {
    currentUser = { full_name: "Admin User", email: "admin@example.com", role: "ADMIN" };
    logout.mockResolvedValue(undefined);
    render(<Home />);
    expect(screen.getByText("Admin User")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Выйти" }));
    await waitFor(() => expect(logout).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith("/login");
  });
  it("shows operational warnings without pretending to know worker readiness", async () => {
    currentUser = { full_name: "Admin", email: "admin@example.com", role: "ADMIN" };
    systemStatus.mockResolvedValue({ tasks: {}, agent_runs: {}, stuck_tasks: 0, pending_approvals: 0, last_activity_at: null, overdue_publications: 2, stalled_ready_auto_ai_tasks: 1, publishing_providers_enabled: { telegram: false, vk: true }, publications: { failed: 1, reconciliation_required: 1, provider_disabled: 2 } });
    render(<Home />);
    expect(await screen.findByText(/Просроченные публикации: 2/)).toBeInTheDocument();
    expect(screen.getByText(/неподтверждённая доставка/)).toBeInTheDocument();
    expect(screen.getByText(/READY AI-задачи долго/)).toBeInTheDocument();
    expect(screen.getByText(/Provider отключён для запланированных/)).toBeInTheDocument();
    expect(screen.getByText(/Runtime heartbeat не подтверждён/)).toBeInTheDocument();
  });

});
