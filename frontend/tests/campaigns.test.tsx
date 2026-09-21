import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import CampaignDetailsPage from "../app/campaigns/[id]/page";
import EditCampaignPage from "../app/campaigns/[id]/edit/page";
import NewCampaignPage from "../app/campaigns/new/page";
import CampaignsPage from "../app/campaigns/page";

const { replace, push, list, get, create, update, archive, taskList } = vi.hoisted(() => ({
  replace: vi.fn(), push: vi.fn(), list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), archive: vi.fn(), taskList: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push }), useParams: () => ({ id: "campaign-1" }) }));
vi.mock("@/components/auth-provider", () => ({ useAuth: () => ({ user: { role: "ADMIN" }, loading: false }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, campaignsApi: { list, get, create, update, archive }, tasksApi: { ...actual.tasksApi, list: taskList } };
});

const campaign = {
  id: "campaign-1", name: "AI-диагностика", description: "Контекст", goal: "30 заявок",
  product: "Диагностика", target_audience: "CEO", offer: "Онлайн-диагностика",
  desired_result: "30 заявок", start_date: "2026-10-01", end_date: "2026-10-31",
  status: "DRAFT" as const, strategy: null, created_by: "user-1",
  creator: { id: "user-1", full_name: "Admin", email: "admin@example.com" },
  created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z",
};

describe("Campaigns UI", () => {
  beforeEach(() => { vi.clearAllMocks(); list.mockResolvedValue([campaign]); get.mockResolvedValue(campaign); taskList.mockResolvedValue([]); vi.spyOn(window, "confirm").mockReturnValue(true); });

  it("renders campaign list and empty state", async () => {
    const first = render(<CampaignsPage />);
    expect(await screen.findByText("AI-диагностика")).toBeInTheDocument();
    first.unmount(); list.mockResolvedValue([]); render(<CampaignsPage />);
    expect(await screen.findByText("Кампаний пока нет")).toBeInTheDocument();
  });

  it("shows campaign list API error", async () => {
    list.mockRejectedValue(new Error("Ошибка загрузки")); render(<CampaignsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка загрузки");
  });

  it("validates required fields and redirects after creation", async () => {
    create.mockResolvedValue(campaign); render(<NewCampaignPage />);
    fireEvent.click(screen.getByRole("button", { name: "Создать кампанию" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Название обязательно.");
    fireEvent.change(screen.getByLabelText("Название *"), { target: { value: "AI-диагностика" } });
    fireEvent.change(screen.getByLabelText("Цель *"), { target: { value: "30 заявок" } });
    fireEvent.click(screen.getByRole("button", { name: "Создать кампанию" }));
    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(push).toHaveBeenCalledWith("/campaigns/campaign-1");
  });

  it("renders details and archives after confirmation", async () => {
    archive.mockResolvedValue({ ...campaign, status: "ARCHIVED" }); render(<CampaignDetailsPage />);
    expect(await screen.findByRole("heading", { name: "AI-диагностика" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Архивировать" }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith("campaign-1"));
    expect(screen.queryByText("Редактировать")).not.toBeInTheDocument();
  });

  it("keeps archived campaign read-only", async () => {
    get.mockResolvedValue({ ...campaign, status: "ARCHIVED" }); render(<CampaignDetailsPage />);
    expect(await screen.findByText("Архив")).toBeInTheDocument();
    expect(screen.queryByText("Редактировать")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Архивировать" })).not.toBeInTheDocument();
  });

  it("edits campaign and redirects", async () => {
    update.mockResolvedValue({ ...campaign, desired_result: "40 заявок" }); render(<EditCampaignPage />);
    const result = await screen.findByLabelText("Желаемый результат");
    fireEvent.change(result, { target: { value: "40 заявок" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("campaign-1", expect.objectContaining({ desired_result: "40 заявок" })));
    expect(push).toHaveBeenCalledWith("/campaigns/campaign-1");
  });

  it("shows readable invalid date error", async () => {
    render(<NewCampaignPage />);
    fireEvent.change(screen.getByLabelText("Название *"), { target: { value: "Campaign" } });
    fireEvent.change(screen.getByLabelText("Цель *"), { target: { value: "Goal" } });
    fireEvent.change(screen.getByLabelText("Дата начала"), { target: { value: "2026-10-31" } });
    fireEvent.change(screen.getByLabelText("Дата окончания"), { target: { value: "2026-10-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Создать кампанию" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Дата окончания не может быть раньше даты начала.");
  });
});
