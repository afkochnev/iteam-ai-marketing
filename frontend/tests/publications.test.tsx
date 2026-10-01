import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PublicationsPage from "../app/publications/page";

const mocks = vi.hoisted(() => ({ campaigns: vi.fn(), calendar: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/components/auth-provider", () => ({ useAuth: () => ({ user: { role: "ADMIN" }, loading: false }) }));
vi.mock("@/lib/api", async (original) => {
  const actual = await original<typeof import("@/lib/api")>();
  return { ...actual, campaignsApi: { ...actual.campaignsApi, list: mocks.campaigns }, publicationsApi: { ...actual.publicationsApi, calendar: mocks.calendar } };
});

describe("publication calendar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.campaigns.mockResolvedValue([{ id: "campaign-1", name: "Запуск" }]);
    mocks.calendar.mockResolvedValue([
      { publication_id: "next", content_item_id: "post-1", content_version_id: "v1", title: "Ближайший пост", channel: "VK", status: "SCHEDULED", scheduled_at: "2026-10-03T10:00:00Z", published_at: null, external_url: null, provider_enabled: true, failure_code: null },
      { publication_id: "done", content_item_id: "post-2", content_version_id: "v2", title: "Опубликованный пост", channel: "TELEGRAM", status: "PUBLISHED", scheduled_at: null, published_at: "2026-10-01T10:00:00Z", external_url: null, provider_enabled: true, failure_code: null },
    ]);
  });

  it("separates upcoming work from publication history and links back to campaign context", async () => {
    render(<PublicationsPage />);
    expect(await screen.findByRole("heading", { name: "Предстоящие" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ближайший пост" })).toHaveAttribute("href", "/content/post-1");
    expect(screen.getByText("Опубликованные · 1")).toBeInTheDocument();
    expect(screen.getByText("Запуск")).toHaveAttribute("href", "/campaigns/campaign-1");
    expect(mocks.calendar).toHaveBeenCalledWith("campaign-1", expect.any(String), expect.any(String));
  });
});
