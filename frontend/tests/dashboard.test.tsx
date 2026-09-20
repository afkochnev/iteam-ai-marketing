import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import Home from "../app/page";

const replace = vi.fn();
const logout = vi.fn();
let currentUser: { full_name: string; email: string; role: "ADMIN" } | null;
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({ user: currentUser, loading: false, login: vi.fn(), logout }),
}));

describe("Protected dashboard", () => {
  beforeEach(() => { vi.clearAllMocks(); currentUser = null; });

  it("redirects an unauthenticated visitor", async () => {
    render(<Home />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("shows the real user and logs out", async () => {
    currentUser = { full_name: "Admin User", email: "admin@example.com", role: "ADMIN" };
    logout.mockResolvedValue(undefined);
    render(<Home />);
    expect(screen.getByText("Admin User")).toBeInTheDocument();
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Выйти" }));
    await waitFor(() => expect(logout).toHaveBeenCalled());
    expect(replace).toHaveBeenCalledWith("/login");
  });
});
