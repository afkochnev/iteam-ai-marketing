import { describe, it, expect } from "vitest";
import { cancelledCards, currentPublication } from "../lib/publication-schedule";
import type { Publication } from "../lib/api";
const row = (id: string, status: Publication["status"], date: string): Publication => ({ id, status, created_at: date, scheduled_at: date } as Publication);
describe("effective publication schedule", () => {
  it("ignores a cancelled original even when returned first", () => {
    const old = row("old", "CANCELLED", "2026-10-26T10:00:00Z");
    const moved = row("new", "SCHEDULED", "2026-10-29T10:00:00Z");
    expect(currentPublication([old, moved])).toBe(moved);
    expect(currentPublication([old])).toBeUndefined();
  });
  it("retains published delivery as the result", () => {
    const delivered = row("sent", "PUBLISHED", "2026-10-26T10:00:00Z");
    expect(currentPublication([row("old", "CANCELLED", "2026-10-25T10:00:00Z"), delivered])).toBe(delivered);
  });
  it("groups cancelled cards without conflating different materials or channels", () => {
    const base = { content_item_id: "post", channel: "VK", scheduled_at: "2026-10-25T10:00:00Z", publication_id: "old" };
    const newer = { ...base, publication_id: "new", scheduled_at: "2026-10-26T10:00:00Z" };
    const rows = [base, newer, { ...base, channel: "TELEGRAM" }, { ...base, content_item_id: "other" }];
    expect(cancelledCards(rows)).toEqual([newer, rows[2], rows[3]]);
    expect(rows).toHaveLength(4);
  });
});
