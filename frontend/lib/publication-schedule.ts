import type { Publication } from "./api";

// A cancelled record never represents the effective schedule.
export function currentPublication(rows: Publication[]): Publication | undefined {
  const priority: Record<string, number> = { PUBLISHING: 0, SCHEDULED: 1, PUBLISHED: 2, FAILED: 3, APPROVED: 4, WAITING_APPROVAL: 5, DRAFT: 6 };
  return rows.filter((row) => row.status !== "CANCELLED").sort((a, b) =>
    (priority[a.status] ?? 9) - (priority[b.status] ?? 9)
    || Date.parse(b.created_at) - Date.parse(a.created_at)
    || a.id.localeCompare(b.id)
  )[0];
}

// Group delivery history by exact material and channel, never by title.
export function cancelledCards<T extends { content_item_id: string; channel: string; scheduled_at: string | null; publication_id: string }>(rows: T[]): T[] {
  const groups = new Map<string, T>();
  for (const row of rows) {
    const key = JSON.stringify([row.content_item_id, row.channel]);
    const existing = groups.get(key);
    if (!existing || Date.parse(row.scheduled_at ?? "1970-01-01") > Date.parse(existing.scheduled_at ?? "1970-01-01")
      || (row.scheduled_at === existing.scheduled_at && row.publication_id > existing.publication_id)) groups.set(key, row);
  }
  return [...groups.values()];
}
