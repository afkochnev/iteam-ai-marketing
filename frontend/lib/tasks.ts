import type { TaskPriority, TaskStatus, TaskType } from "./api";
import { formatDateTime } from "./campaigns";

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = { NEW: "Новая", BLOCKED: "Заблокирована", READY: "Готова", IN_PROGRESS: "В работе", WAITING_REVIEW: "Ожидает проверки", WAITING_APPROVAL: "Ожидает согласования", APPROVED: "Согласована", COMPLETED: "Завершена", FAILED: "Ошибка", CANCELLED: "Отменена" };
export const TASK_PRIORITY_LABELS: Record<TaskPriority, string> = { LOW: "Низкий", NORMAL: "Обычный", HIGH: "Высокий", URGENT: "Срочный" };
export const TASK_TYPE_LABELS: Record<TaskType, string> = { CAMPAIGN_PLANNING: "Планирование кампании", KNOWLEDGE_RESEARCH: "Поиск знаний", WRITE_ARTICLE: "Написание статьи", CREATE_SOCIAL_POSTS: "Создание публикаций", CONTENT_REVISION: "Доработка контента", ANALYZE_PERFORMANCE: "Анализ результатов", MANUAL: "Ручная задача" };
export const TASK_STATUSES = Object.keys(TASK_STATUS_LABELS) as TaskStatus[];
export const TASK_PRIORITIES = Object.keys(TASK_PRIORITY_LABELS) as TaskPriority[];
export const TASK_TYPES = (Object.keys(TASK_TYPE_LABELS) as TaskType[]).filter((type) => type !== "ANALYZE_PERFORMANCE");
export function taskDate(value: string | null | undefined) { return value?.trim() ? formatDateTime(value) : "—"; }

export const AUTO_TASK_TYPES: TaskType[] = ["KNOWLEDGE_RESEARCH", "WRITE_ARTICLE", "CREATE_SOCIAL_POSTS", "CONTENT_REVISION", "ANALYZE_PERFORMANCE"];
