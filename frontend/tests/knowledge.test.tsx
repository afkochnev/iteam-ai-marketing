import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import KnowledgePage from "@/app/knowledge/page";
import { useAuth } from "@/components/auth-provider";
import { knowledgeApi } from "@/lib/api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }), useSearchParams: () => new URLSearchParams() }));
vi.mock("@/components/auth-provider", () => ({ useAuth: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, knowledgeApi: { getStore: vi.fn(), initializeStore: vi.fn(), listItems: vi.fn(), upload: vi.fn(), retry: vi.fn(), archive: vi.fn(), search: vi.fn() } };
});

const admin = { id: "u1", email: "admin@example.com", full_name: "Admin", role: "ADMIN" as const, is_active: true, created_at: "2026-09-21T00:00:00Z" };

describe("Knowledge page", () => {
  beforeEach(() => {
    vi.mocked(useAuth).mockReturnValue({ user: admin, loading: false, login: vi.fn(), logout: vi.fn() });
    vi.mocked(knowledgeApi.getStore).mockResolvedValue({ id: "s1", provider: "OPENAI", name: "iTeam Knowledge Base", external_store_id: "vs_1", status: "ACTIVE", is_active: true, created_at: "2026-09-21T00:00:00Z" });
    vi.mocked(knowledgeApi.listItems).mockResolvedValue([]);
    vi.mocked(knowledgeApi.search).mockResolvedValue({ query: "ритм", result_count: 1, results: [{ result_key: "a".repeat(64), knowledge_item_id: "k1", source_id: "src1", source_title: "Ручные загрузки", filename: "management.md", file_id: "file_1", excerpt: "управленческий ритм", score: 0.91, metadata: {} }] });
  });

  it("renders empty state and provenance search result", async () => {
    render(<KnowledgePage />);
    expect(await screen.findByText("Документов пока нет.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Запрос"), { target: { value: "ритм" } });
    fireEvent.click(screen.getByRole("button", { name: "Найти" }));
    expect(await screen.findByText("management.md")).toBeInTheDocument();
    expect(screen.getByText(/0.910/)).toBeInTheDocument();
    expect(screen.getByText("управленческий ритм")).toBeInTheDocument();
  });

  it("hides admin controls from manager", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: { ...admin, role: "MANAGER" }, loading: false, login: vi.fn(), logout: vi.fn() });
    render(<KnowledgePage />);
    await waitFor(() => expect(knowledgeApi.listItems).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Загрузить документ" })).not.toBeInTheDocument();
  });
});
