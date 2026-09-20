import { afterEach, describe, expect, it, vi } from "vitest";

import { authApi } from "../lib/api";

describe("auth API", () => {
  afterEach(() => vi.restoreAllMocks());

  it("uses cookie credentials for login", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ user: { email: "admin@example.com" } }), { status: 200 })
    );
    await authApi.login("admin@example.com", "secret");
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/auth/login"), expect.objectContaining({ credentials: "include" }));
  });

  it("surfaces backend errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "Неверный email или пароль." } }), { status: 401 })
    );
    await expect(authApi.login("admin@example.com", "wrong")).rejects.toThrow("Неверный email или пароль.");
  });
});
