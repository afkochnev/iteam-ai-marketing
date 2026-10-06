import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import ApprovalsPage from "../app/approvals/page";
const list = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ approvalsApi: { list } }));
beforeEach(() => list.mockReset());
it("shows loading before empty and provides a useful empty-state route", async () => {
  list.mockResolvedValue([]); render(<ApprovalsPage />);
  expect(screen.getByRole("status")).toHaveTextContent("Загружаем");
  expect(screen.queryByText(/Согласований пока нет/)).not.toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "Открыть контент" })).toHaveAttribute("href", "/content");
});
it("retries a read error and links to exact approval anchors", async () => {
  list.mockRejectedValueOnce(new Error("offline")).mockResolvedValue([{ id: "a1", object_type: "CONTENT_ITEM", object_id: "post-1", subject_version: 3, status: "PENDING", created_at: "2026-10-05T12:00:00Z" }]);
  render(<ApprovalsPage />); expect(await screen.findByRole("alert")).toHaveTextContent("offline"); fireEvent.click(screen.getByRole("button", { name: "Повторить загрузку" }));
  expect(await screen.findByRole("link", { name: "Открыть материал" })).toHaveAttribute("href", "/content/post-1#approval");
});
