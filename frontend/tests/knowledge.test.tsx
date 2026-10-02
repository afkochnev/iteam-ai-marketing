import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import KnowledgePage from "@/app/knowledge/page";
import { useAuth } from "@/components/auth-provider";
import { knowledgeApi, knowledgePacksApi, type KnowledgeItem } from "@/lib/api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }), useSearchParams: () => new URLSearchParams() }));
vi.mock("@/components/auth-provider", () => ({ useAuth: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    knowledgeApi: { getStore: vi.fn(), initializeStore: vi.fn(), listSources: vi.fn(), listItems: vi.fn(), upload: vi.fn(), retry: vi.fn(), archive: vi.fn(), search: vi.fn() },
    knowledgePacksApi: { list: vi.fn() },
  };
});

const admin = { id: "u1", email: "admin@example.com", full_name: "Admin", role: "ADMIN" as const, is_active: true, created_at: "2026-09-21T00:00:00Z" };

describe("Knowledge page", () => {
  beforeEach(() => {
    vi.mocked(useAuth).mockReturnValue({ user: admin, loading: false, login: vi.fn(), logout: vi.fn() });
    vi.mocked(knowledgeApi.getStore).mockResolvedValue({ id: "s1", provider: "OPENAI", name: "iTeam Knowledge Base", external_store_id: "vs_1", status: "ACTIVE", is_active: true, created_at: "2026-09-21T00:00:00Z" });
    vi.mocked(knowledgeApi.listItems).mockResolvedValue([]);
    vi.mocked(knowledgeApi.listSources).mockResolvedValue([{ id: "src1", name: "Ручные загрузки", source_type: "FILE_UPLOAD", source_url: null, status: "ACTIVE" }]);
    vi.mocked(knowledgePacksApi.list).mockResolvedValue([]);
    vi.mocked(knowledgeApi.search).mockResolvedValue({ query: "ритм", result_count: 1, results: [{ result_key: "a".repeat(64), knowledge_item_id: "k1", source_id: "src1", source_title: "Ручные загрузки", filename: "management.md", file_id: "file_1", excerpt: "управленческий ритм", score: 0.91, metadata: {} }] });
  });

  it("renders empty state and provenance search result", async () => {
    render(<KnowledgePage />);
    expect(await screen.findByText("Документов пока нет.")).toBeInTheDocument();
    expect(screen.getByText("Загрузка файла · документов: 0")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Хранитель знаний" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Запрос"), { target: { value: "ритм" } });
    fireEvent.click(screen.getByRole("button", { name: "Найти" }));
    expect(await screen.findByText("management.md")).toBeInTheDocument();
    expect(screen.getByText(/0.910/)).toBeInTheDocument();
    expect(screen.getByText("управленческий ритм")).toBeInTheDocument();
  });

  it("shows upload progress and completion feedback", async () => {
    let resolveUpload!: (value: KnowledgeItem) => void;
    const uploadResult = new Promise<KnowledgeItem>((resolve) => { resolveUpload = resolve; });
    vi.mocked(knowledgeApi.upload).mockReturnValue(uploadResult);
    render(<KnowledgePage />);
    await screen.findByText("Документов пока нет.");
    fireEvent.change(screen.getByLabelText("Файл PDF, DOCX, TXT или MD"), { target: { files: [new File(["content"], "source.md", { type: "text/markdown" })] } });
    const uploadForm = screen.getByRole("heading", { name: "Загрузить документ" }).closest("form");
    expect(uploadForm).not.toBeNull();
    fireEvent.submit(uploadForm!);
    expect(await screen.findByRole("button", { name: "Загружаем документ…" })).toBeDisabled();
    resolveUpload({ id: "k1", source_id: "src1", title: "source", author: null, content_type: "text/markdown", original_filename: "source.md", mime_type: "text/markdown", file_size_bytes: 7, source_url: null, openai_file_id: null, vector_store_file_id: null, status: "UPLOADING", metadata: {}, error_code: null, error_message: null, created_by: "u1", created_at: "2026-09-21T00:00:00Z", updated_at: "2026-09-21T00:00:00Z", indexed_at: null, archived_at: null });
    expect(await screen.findByText("Документ загружен и поставлен в очередь индексирования.")).toBeInTheDocument();
  });

  it("hides admin controls from manager", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: { ...admin, role: "MANAGER" }, loading: false, login: vi.fn(), logout: vi.fn() });
    render(<KnowledgePage />);
    await waitFor(() => expect(knowledgeApi.listItems).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Загрузить документ" })).not.toBeInTheDocument();
  });
});
