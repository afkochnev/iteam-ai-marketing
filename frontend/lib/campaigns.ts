import type { CampaignStatus } from "@/lib/api";

export const CAMPAIGN_STATUS_LABELS: Record<CampaignStatus, string> = {
  DRAFT: "Черновик",
  PLANNING: "Планирование",
  WAITING_APPROVAL: "Ожидает согласования",
  ACTIVE: "Активна",
  PAUSED: "Приостановлена",
  COMPLETED: "Завершена",
  ARCHIVED: "Архив",
};

const DATE_ONLY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;
const ISO_DATE_PREFIX_PATTERN = /^(\d{4})-(\d{2})-(\d{2})(?:T|$)/;
const ISO_DATETIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})?$/i;

type ParsedDate =
  | { kind: "missing" }
  | { kind: "invalid" }
  | { kind: "valid"; date: Date };

function hasValidCalendarDate(value: string): boolean {
  const match = ISO_DATE_PREFIX_PATTERN.exec(value);
  if (!match) return false;

  const [, yearText, monthText, dayText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(0, 0, 0, 0);
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

function parseDate(value: string | null | undefined): ParsedDate {
  const normalized = value?.trim();
  if (!normalized) return { kind: "missing" };

  const dateOnly = DATE_ONLY_PATTERN.test(normalized);
  if ((!dateOnly && !ISO_DATETIME_PATTERN.test(normalized)) || !hasValidCalendarDate(normalized)) {
    return { kind: "invalid" };
  }

  const date = new Date(dateOnly ? `${normalized}T00:00:00` : normalized);
  return Number.isNaN(date.getTime()) ? { kind: "invalid" } : { kind: "valid", date };
}

function formatParsedDate(
  parsed: ParsedDate,
  formatter: Intl.DateTimeFormat,
): string {
  if (parsed.kind === "missing") return "Дата не указана";
  if (parsed.kind === "invalid") return "Некорректная дата";

  try {
    return formatter.format(parsed.date);
  } catch {
    return "Некорректная дата";
  }
}

export function formatDate(value: string | null | undefined): string {
  return formatParsedDate(parseDate(value), new Intl.DateTimeFormat("ru-RU"));
}

export function formatDateTime(value: string | null | undefined): string {
  return formatParsedDate(
    parseDate(value),
    new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }),
  );
}
