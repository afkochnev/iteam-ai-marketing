import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import DirectorPage from "../app/campaigns/[id]/director/page";
import { verifiedHref } from "../lib/director-chat";
const id = "11111111-1111-4111-8111-111111111111";
const conversation = { id: "conversation", title: "Сохранённый диалог", archived_at: null };
const userMessage = { id: "user", role: "USER", status: "COMPLETED", content: "Что дальше?", references: [], limitations: [] };
const assistant = { id: "assistant", role: "ASSISTANT", status: "COMPLETED", content: "Утвердите стратегию.", references: [], limitations: [] };
const mocks = vi.hoisted(() => ({ get: vi.fn(), list: vi.fn(), create: vi.fn(), messages: vi.fn(), send: vi.fn(), retry: vi.fn(), archive: vi.fn(), replace: vi.fn(), user: {} as object | null }));
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "11111111-1111-4111-8111-111111111111" }), useRouter: () => ({ replace: mocks.replace }) }));
vi.mock("@/components/auth-provider", () => ({ useAuth: () => ({ user: mocks.user, loading: false }) }));
vi.mock("@/lib/api", () => ({ campaignsApi: { get: mocks.get } }));
vi.mock("@/lib/director-chat", async (original) => ({ ...(await original<object>()), directorChatApi: mocks }));
beforeEach(() => {
  vi.clearAllMocks(); mocks.user = {}; mocks.get.mockResolvedValue({ id, name: "Кампания", status: "ACTIVE" });
  mocks.list.mockResolvedValue([conversation]); mocks.messages.mockResolvedValue([userMessage, assistant]);
  mocks.create.mockResolvedValue({ ...conversation, id: "new", title: "Новый диалог" });
  mocks.send.mockResolvedValue({ user_message: userMessage, assistant_message: { ...assistant, status: "PENDING", content: "" } });
  mocks.retry.mockResolvedValue({ ...assistant, status: "PENDING", content: "" });
  mocks.archive.mockResolvedValue({ ...conversation, archived_at: "2026-10-07" });
});
afterEach(() => vi.useRealTimers());
async function ready() { render(<DirectorPage />); await screen.findByText("Утвердите стратегию."); }
it("loads persisted conversation and chronological history", async () => {
  await ready(); expect(mocks.list).toHaveBeenCalledWith(id); expect(mocks.messages).toHaveBeenCalledWith("conversation");
  expect(screen.getByText("Что дальше?")).toBeInTheDocument(); expect(screen.getByRole("link", { name: "К кампании" })).toHaveAttribute("href", `/campaigns/${id}`);
});
it("creates a new persistent conversation", async () => {
  await ready(); fireEvent.click(screen.getByRole("button", { name: "Новый диалог" }));
  await waitFor(() => expect(mocks.create).toHaveBeenCalledWith(id)); await waitFor(() => expect(mocks.messages).toHaveBeenCalledWith("new"));
});
it("sends with UUID idempotency key and shows pending", async () => {
  await ready(); fireEvent.change(screen.getByLabelText("Ваш вопрос"), { target: { value: "Что дальше?" } }); fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
  expect(await screen.findByText("Директор готовит ответ…")).toBeInTheDocument();
  expect(mocks.send).toHaveBeenCalledWith("conversation", "Что дальше?", expect.stringMatching(/^[a-f0-9-]{36}$/));
  expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
});
it("reuses client key when HTTP send fails", async () => {
  await ready(); mocks.send.mockRejectedValueOnce(new Error("Network"));
  fireEvent.change(screen.getByLabelText("Ваш вопрос"), { target: { value: "Question" } }); fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
  await screen.findByText("Network"); fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
  await screen.findByText("Директор готовит ответ…"); expect(mocks.send.mock.calls[0][2]).toBe(mocks.send.mock.calls[1][2]);
});
it("polls pending and stops after completion", async () => {
  mocks.messages.mockResolvedValueOnce([{ ...assistant, status: "PENDING", content: "" }]).mockResolvedValue([assistant]);
  vi.useFakeTimers(); render(<DirectorPage />); await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("Директор готовит ответ…")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
  expect(screen.getByText("Утвердите стратегию.")).toBeInTheDocument(); const count = mocks.messages.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); }); expect(mocks.messages).toHaveBeenCalledTimes(count);
});
it("completed history does not poll", async () => {
  await ready(); vi.useFakeTimers(); await act(async () => { await vi.advanceTimersByTimeAsync(6000); }); expect(mocks.messages).toHaveBeenCalledTimes(1);
});
it("renders verified references and separate limitations", async () => {
  mocks.messages.mockResolvedValue([{ ...assistant, references: [{ entity_type: "campaign", entity_id: id, label: "Текущая кампания", href: `/campaigns/${id}` }], limitations: ["Нет метрик"] }]);
  await ready(); expect(screen.getByRole("link", { name: "Текущая кампания" })).toHaveAttribute("href", `/campaigns/${id}`); expect(screen.getByText("Ограничения ответа")).toBeInTheDocument(); expect(screen.getByText("Нет метрик")).toBeInTheDocument();
});
it("does not trust raw URLs or render model HTML", async () => {
  mocks.messages.mockResolvedValue([{ ...assistant, content: '<img src=x onerror="evil()"> https://evil.test', references: [{ entity_type: "campaign", entity_id: id, label: "Fake", href: "https://evil.test" }] }]);
  render(<DirectorPage />); await screen.findByText(/<img src=x/); expect(screen.queryByRole("img")).not.toBeInTheDocument(); expect(screen.queryByRole("link", { name: "Fake" })).not.toBeInTheDocument();
  expect(verifiedHref({ entity_type: "campaign", entity_id: "../evil", label: "Fake", href: "/campaigns/../evil" }, id)).toBeNull();
});
it("shows safe failure and retries the same message", async () => {
  mocks.messages.mockResolvedValue([{ ...assistant, status: "FAILED", content: "", error_message: "Не удалось получить ответ." }]);
  render(<DirectorPage />); await screen.findByText("Не удалось получить ответ."); fireEvent.click(screen.getByRole("button", { name: "Повторить ответ" }));
  await screen.findByText("Директор готовит ответ…"); expect(mocks.retry).toHaveBeenCalledWith("conversation", "assistant");
});
it("history survives remount", async () => {
  const first = render(<DirectorPage />); await screen.findByText("Утвердите стратегию."); first.unmount(); await ready(); expect(mocks.messages).toHaveBeenCalledTimes(2);
});
it("shows advisory notice and no business mutation controls", async () => {
  await ready(); expect(screen.getByText(/Консультативный режим/)).toHaveTextContent("не изменяет данные без отдельного подтверждённого workflow");
  for (const name of ["Применить", "Изменить стратегию", "Создать задачу", "Пересмотреть план", "Опубликовать"]) expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
});
it("archived campaign reads history but prevents send and new dialog", async () => {
  mocks.get.mockResolvedValue({ id, name: "Кампания", status: "ARCHIVED" }); await ready(); expect(screen.getByRole("button", { name: "Новый диалог" })).toBeDisabled(); expect(screen.getByLabelText("Ваш вопрос")).toBeDisabled(); expect(screen.getByText("Архив доступен только для чтения.")).toBeInTheDocument();
});
it("archives own dialog and keeps readable history", async () => {
  await ready(); fireEvent.click(screen.getByRole("button", { name: "Архивировать диалог" })); await screen.findByText("Архив доступен только для чтения."); expect(screen.getByText("Утвердите стратегию.")).toBeInTheDocument();
});
it("redirects unauthenticated users", async () => {
  mocks.user = null; render(<DirectorPage />); await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login")); expect(mocks.list).not.toHaveBeenCalled();
});
