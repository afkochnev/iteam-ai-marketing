import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RegisterPage from "../app/register/page";
import ProfilePage from "../app/profile/page";
const mocks = vi.hoisted(() => ({ capabilities: vi.fn(), register: vi.fn(), changePassword: vi.fn(), replace: vi.fn(), user: { full_name: "User", email: "user@example.com", role: "MANAGER" } as object | null }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: mocks.replace }) }));
vi.mock("@/components/auth-provider", () => ({ useAuth: () => ({ user: mocks.user, loading: false }) }));
vi.mock("@/lib/api", () => ({ authApi: mocks }));
function fillRegistration(password = "secure-password-24", confirmation = password) {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "New@Example.com" } });
  fireEvent.change(screen.getByLabelText("Полное имя"), { target: { value: "New User" } });
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("Подтверждение пароля"), { target: { value: confirmation } });
  fireEvent.click(screen.getByRole("button", { name: "Зарегистрироваться" }));
}
function fillChange(current = "valid-password", password = "secure-password-24") {
  fireEvent.change(screen.getByLabelText("Текущий пароль"), { target: { value: current } });
  fireEvent.change(screen.getByLabelText("Новый пароль"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("Подтверждение пароля"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "Изменить пароль" }));
}
describe("Accounts", () => {
  beforeEach(() => { vi.clearAllMocks(); mocks.user = { full_name: "User", email: "user@example.com", role: "MANAGER" }; mocks.capabilities.mockResolvedValue({ self_registration_enabled: true }); mocks.register.mockResolvedValue({}); mocks.changePassword.mockResolvedValue({ message: "Пароль изменён. Другие сессии завершены." }); });
  it("explains disabled registration", async () => {
    mocks.capabilities.mockResolvedValue({ self_registration_enabled: false }); render(<RegisterPage />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("регистрация отключена")); expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
  it.each([["short", "short", "от 12 до 128"], ["secure-password-24", "different-password", "не совпадают"]])("validates registration %s", async (password, confirmation, message) => {
    render(<RegisterPage />); await screen.findByLabelText("Email"); fillRegistration(password, confirmation);
    expect(screen.getByRole("alert")).toHaveTextContent(message); expect(mocks.register).not.toHaveBeenCalled();
  });
  it("registers then directs to login", async () => {
    render(<RegisterPage />); await screen.findByLabelText("Email"); fillRegistration();
    expect(await screen.findByText(/Регистрация завершена/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Войти с новым паролем" })).toHaveAttribute("href", "/login");
    expect(mocks.register).toHaveBeenCalledWith("new@example.com", "secure-password-24", "New User");
  });
  it("shows registration backend error", async () => {
    mocks.register.mockRejectedValue(new Error("Этот email недоступен для регистрации.")); render(<RegisterPage />); await screen.findByLabelText("Email"); fillRegistration();
    expect(await screen.findByRole("alert")).toHaveTextContent("email недоступен");
  });
  it("changes password and clears inputs", async () => {
    render(<ProfilePage />); expect(screen.getByText("user@example.com")).toBeInTheDocument(); fillChange();
    expect(await screen.findByRole("status")).toHaveTextContent("Пароль изменён");
    expect(mocks.changePassword).toHaveBeenCalledWith("valid-password", "secure-password-24"); expect(screen.getByLabelText("Текущий пароль")).toHaveValue("");
  });
  it("shows wrong current password", async () => {
    mocks.changePassword.mockRejectedValue(new Error("Текущий пароль неверен.")); render(<ProfilePage />); fillChange(); expect(await screen.findByRole("alert")).toHaveTextContent("Текущий пароль неверен");
  });
  it("rejects unchanged password", () => {
    render(<ProfilePage />); fillChange("valid-password", "valid-password"); expect(screen.getByRole("alert")).toHaveTextContent("должен отличаться"); expect(mocks.changePassword).not.toHaveBeenCalled();
  });
  it("requires authentication", async () => {
    mocks.user = null; render(<ProfilePage />); await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login")); expect(screen.queryByLabelText("Текущий пароль")).not.toBeInTheDocument();
  });
});
