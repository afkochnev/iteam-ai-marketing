import { describe, expect, it } from "vitest";

import { formatDate, formatDateTime } from "../lib/campaigns";

describe("defensive date formatters", () => {
  it("formats a date-only value", () => {
    expect(formatDate("2026-10-06")).toBe(new Intl.DateTimeFormat("ru-RU").format(new Date("2026-10-06T00:00:00")));
  });

  it("formats ISO datetimes without appending a second time suffix", () => {
    const value = "2026-10-06T12:00:00Z";
    expect(formatDate(value)).toBe(new Intl.DateTimeFormat("ru-RU").format(new Date(value)));
    expect(formatDateTime(value)).toBe(new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)));
  });

  it("formats ISO datetimes with a timezone offset", () => {
    const value = "2026-10-06T23:30:00-02:00";
    expect(formatDate(value)).toBe(new Intl.DateTimeFormat("ru-RU").format(new Date(value)));
    expect(formatDateTime(value)).toBe(new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)));
  });

  it.each([null, undefined, "", "   "]) ("uses a neutral fallback for missing values (%s)", (value) => {
    expect(formatDate(value)).toBe("Дата не указана");
    expect(formatDateTime(value)).toBe("Дата не указана");
  });

  it.each(["not-a-date", "2026-02-30", "2026-10-06T25:61:00Z"]) ("uses a neutral fallback for malformed values (%s)", (value) => {
    expect(formatDate(value)).toBe("Некорректная дата");
    expect(formatDateTime(value)).toBe("Некорректная дата");
  });
});
