import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AgentDetailsPage from "../app/agents/[id]/page";
import AgentsPage from "../app/agents/page";

const { replace, list, get, update, updateTool } = vi.hoisted(() => ({
  replace: vi.fn(), list: vi.fn(), get: vi.fn(), update: vi.fn(), updateTool: vi.fn(),
}));
let role: "ADMIN" | "MANAGER" = "ADMIN";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useParams: () => ({ id: "agent-1" }),
}));
vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({ user: { role }, loading: false }),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, agentsApi: { list, get, update, updateTool } };
});

const agent = {
  id: "agent-1", name: "Writer", slug: "writer", role: "content_writer",
  description: "Создаёт статьи", model: null, status: "ACTIVE" as const,
  autonomy_level: 2, system_prompt: "Исходный prompt", settings: {},
  created_at: "2026-09-20T00:00:00Z", updated_at: "2026-09-20T00:00:00Z",
  tools: [{ id: "tool-1", tool_name: "save_content", is_enabled: true, requires_approval: false, settings: {} }],
};

describe("Agents UI", () => {
  beforeEach(() => { vi.clearAllMocks(); role = "ADMIN"; list.mockResolvedValue([agent]); get.mockResolvedValue(agent); });

  it("loads and renders agents", async () => {
    render(<AgentsPage />);
    expect(await screen.findByText("Писатель")).toBeInTheDocument();
    expect(screen.getByText("Модель по умолчанию")).toBeInTheDocument();
  });

  it("shows API errors", async () => {
    list.mockRejectedValue(new Error("Не удалось загрузить агентов."));
    render(<AgentsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось загрузить агентов.");
  });

  it("shows edit controls to Admin and saves changes", async () => {
    update.mockResolvedValue({ ...agent, description: "Новое описание", system_prompt: "Новый prompt" });
    render(<AgentDetailsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Редактировать" }));
    fireEvent.change(screen.getByLabelText("Описание"), { target: { value: "Новое описание" } });
    fireEvent.change(screen.getByLabelText("System prompt"), { target: { value: "Новый prompt" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("agent-1", expect.objectContaining({ description: "Новое описание", system_prompt: "Новый prompt" })));
    expect(await screen.findByText("Изменения сохранены.")).toBeInTheDocument();
  });

  it("hides edit controls from Manager", async () => {
    role = "MANAGER";
    render(<AgentDetailsPage />);
    await screen.findByText("Исходный prompt");
    expect(screen.queryByRole("button", { name: "Редактировать" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Включён")).toBeDisabled();
  });

  it("updates a tool permission", async () => {
    updateTool.mockResolvedValue({ ...agent.tools[0], is_enabled: false });
    render(<AgentDetailsPage />);
    const checkbox = await screen.findByLabelText("Включён");
    fireEvent.click(checkbox);
    await waitFor(() => expect(updateTool).toHaveBeenCalledWith("agent-1", "tool-1", { is_enabled: false }));
  });
});
