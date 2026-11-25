import { afterEach, describe, expect, it, vi } from "vitest";

import { authApi, distributionWorkspaceApi, request, requestBlob } from "../lib/api";

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

describe("authenticated media request", () => {
  afterEach(() => vi.restoreAllMocks());

  it("preserves the authentication error from a media request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "AUTHENTICATION_REQUIRED", message: "Требуется авторизация." } }), { status: 401 })
    );
    const { signal } = new AbortController();
    await expect(requestBlob("/media", signal)).rejects.toMatchObject({
      status: 401, code: "AUTHENTICATION_REQUIRED", message: "Требуется авторизация.",
    });
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/media"), { credentials: "include", signal });
  });

  it("falls back to the media message when the error response is not JSON", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("Bad gateway", { status: 502 }));
    await expect(requestBlob("/media", new AbortController().signal)).rejects.toMatchObject({
      status: 502, message: "Не удалось загрузить изображение.",
    });
  });
});

describe("distribution workspace API", () => {
  afterEach(() => vi.restoreAllMocks());

  it("encodes a bounded campaign page as one cookie-authenticated GET", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
    await distributionWorkspaceApi.get({ campaign_id: "campaign-1", limit: 50, cursor: "opaque+/=" });
    const [url, options] = fetchMock.mock.calls[0];
    const parsed = new URL(String(url));
    expect(parsed.pathname).toBe("/api/v1/distribution-workspace");
    expect(Object.fromEntries(parsed.searchParams)).toEqual({ campaign_id: "campaign-1", limit: "50", cursor: "opaque+/=" });
    expect(options).toEqual(expect.objectContaining({ credentials: "include" }));
    expect(options?.body).toBeUndefined();
    expect(new Headers(options?.headers).has("Content-Type")).toBe(false);
  });

  it("requests a bounded snapshot from an exact publication without a cursor", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
    const publicationId = "11111111-1111-4111-8111-111111111111";
    await distributionWorkspaceApi.get({ campaign_id: "campaign-1", limit: 50, publication_id: publicationId });
    const [url, options] = fetchMock.mock.calls[0];
    const parsed = new URL(String(url));
    expect(parsed.pathname).toBe("/api/v1/distribution-workspace");
    expect(Object.fromEntries(parsed.searchParams)).toEqual({ campaign_id: "campaign-1", limit: "50", publication_id: publicationId });
    expect(options).toEqual(expect.objectContaining({ credentials: "include" }));
    expect(options?.body).toBeUndefined();
  });
});
