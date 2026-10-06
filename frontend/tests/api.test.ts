import { afterEach, describe, expect, it, vi } from "vitest";

import { authApi, request } from "../lib/api";

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

describe("request headers", () => {
  afterEach(() => vi.restoreAllMocks());
  it.each(["GET", "HEAD"])("omits Content-Type for bodyless %s", async (method) => {
    const mock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
    await request("/read", { method });
    expect(new Headers(mock.mock.calls[0][1]!.headers).has("Content-Type")).toBe(false);
    expect(mock.mock.calls[0][1]!.credentials).toBe("include");
  });
  it("adds Content-Type for JSON POST", async () => {
    const mock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
    await request("/write", { method: "POST", body: "{}" });
    expect(new Headers(mock.mock.calls[0][1]!.headers).get("Content-Type")).toBe("application/json");
  });
  it.each([new Headers({ "content-type": "application/custom", "X-Custom": "kept" }), [["content-type", "application/custom"], ["X-Custom", "kept"]] as [string,string][]])("preserves caller headers", async (headers) => {
    const mock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
    await request("/write", { method: "POST", body: "{}", headers });
    const sent = new Headers(mock.mock.calls[0][1]!.headers);
    expect(sent.get("Content-Type")).toBe("application/custom");
    expect(sent.get("X-Custom")).toBe("kept");
  });
  it("leaves FormData header to browser", async () => {
    const mock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
    await request("/upload", { method: "POST", body: new FormData() });
    expect(new Headers(mock.mock.calls[0][1]!.headers).has("Content-Type")).toBe(false);
  });
});
