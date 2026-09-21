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

export function formatDate(value: string | null): string {
  return value ? new Intl.DateTimeFormat("ru-RU").format(new Date(`${value}T00:00:00`)) : "Не задана";
}

export function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}
