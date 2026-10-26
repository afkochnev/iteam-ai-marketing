import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { PublicationReplanningPreview } from "../components/publication-replanning-preview";
const call = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ request: call }));
const row = { publication_id: "one", title: "Пост", channel: "VK" as const, scheduled_at: "2030-10-29T10:00:00Z", campaign: { id: "campaign", name: "Кампания" }, operational_data_complete: true };
describe("replanning preview", () => {
  it("sends only a preview request and clears a stale result after editing", async () => {
    call.mockResolvedValue({ mode: "PREVIEW_ONLY", message: "Расписание не изменено", items: [{ publication_id: "one", original_channel: "VK", original_scheduled_at: row.scheduled_at, proposed_channel: "TELEGRAM", proposed_scheduled_at: row.scheduled_at, blockers: ["Нужно согласование"], warnings: [] }] });
    render(<PublicationReplanningPreview rows={[row]} />);
    fireEvent.click(screen.getByRole("button", { name: "Предпросмотр перепланирования" }));
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(screen.getByLabelText("Новый канал"), { target: { value: "TELEGRAM" } });
    fireEvent.click(screen.getByRole("button", { name: "Проверить изменения без сохранения" }));
    expect(await screen.findByText("Расписание не изменено")).toBeInTheDocument();
    expect(call).toHaveBeenCalledTimes(1);
    expect(call.mock.calls[0][0]).toBe("/publications/campaign/campaign/replanning-preview");
    expect(JSON.parse(call.mock.calls[0][1].body).changes[0].channel).toBe("TELEGRAM");
    fireEvent.change(screen.getByLabelText("Новый канал"), { target: { value: "VK" } });
    await waitFor(() => expect(screen.queryByText("Расписание не изменено")).not.toBeInTheDocument());
  });
});
