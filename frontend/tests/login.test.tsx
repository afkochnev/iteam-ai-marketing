import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import LoginPage from "../app/login/page";

const replace = vi.fn();
const login = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({ user: null, loading: false, login, logout: vi.fn() }),
}));

describe("Login page", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("renders and validates required fields", async () => {
    render(<LoginPage />);
    expect(screen.getByRole("link", { name: "Зарегистрироваться" })).toHaveAttribute("href", "/register");
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Введите email и пароль.");
  });

  it("logs in and redirects", async () => {
    login.mockResolvedValue(undefined);
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    await waitFor(() => expect(login).toHaveBeenCalledWith("admin@example.com", "secret"));
    expect(replace).toHaveBeenCalledWith("/");
  });

  it("shows login error", async () => {
    login.mockRejectedValue(new Error("Неверный email или пароль."));
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@example.com" } });
    fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Неверный email или пароль.");
  });
});
