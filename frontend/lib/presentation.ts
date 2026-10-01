import type { Activity, AgentRunStatus, ContentStatus, ContentType, PublicationPlanStatus, PublicationStatus } from "@/lib/api";
import { TASK_TYPE_LABELS } from "@/lib/tasks";

export const CONTENT_TYPE_LABELS: Record<ContentType, string> = {
  ARTICLE: "Статья",
  SOCIAL_POST: "Пост для соцсетей",
  SOCIAL_POST_PACK: "Пакет постов",
};

export const CONTENT_STATUS_LABELS: Record<ContentStatus, string> = {
  DRAFT: "Черновик",
  WAITING_REVIEW: "Ожидает проверки",
  WAITING_APPROVAL: "Ожидает согласования",
  APPROVED: "Утверждён",
  REJECTED: "Отклонён",
  ARCHIVED: "В архиве",
};

export const PUBLICATION_STATUS_LABELS: Record<PublicationStatus, string> = {
  DRAFT: "Черновик",
  WAITING_APPROVAL: "Ожидает согласования",
  APPROVED: "Разрешена к публикации",
  SCHEDULED: "Запланирована",
  PUBLISHING: "Отправляется",
  PUBLISHED: "Опубликована",
  FAILED: "Ошибка отправки",
  CANCELLED: "Отменена",
};

export const PLAN_STATUS_LABELS: Record<PublicationPlanStatus, string> = {
  DRAFT: "Черновик",
  WAITING_APPROVAL: "Ожидает согласования",
  APPROVED: "Утверждён",
  REJECTED: "Возвращён на доработку",
  ARCHIVED: "В архиве",
};

export const RUN_STATUS_LABELS: Record<AgentRunStatus, string> = {
  QUEUED: "В очереди",
  RUNNING: "Выполняется",
  WAITING_APPROVAL: "Ждёт согласования",
  COMPLETED: "Завершён",
  FAILED: "Ошибка",
  CANCELLED: "Отменён",
};

export { TASK_TYPE_LABELS };

export const ACTIVITY_LABELS: Record<string, string> = {
  PUBLICATION_CREATED: "Создана публикация",
  PUBLICATION_APPROVED: "Публикация разрешена",
  PUBLICATION_SCHEDULED: "Публикация запланирована",
  PUBLICATION_RESCHEDULED: "Дата публикации изменена",
  PUBLICATION_QUEUED: "Публикация поставлена в очередь",
  PUBLICATION_CANCELLED: "Публикация отменена",
  PUBLICATION_STARTED: "Началась отправка публикации",
  PUBLICATION_PUBLISHED: "Материал опубликован",
  PUBLICATION_FAILED: "Не удалось отправить публикацию",
  PUBLICATION_RECONCILED_PUBLISHED: "Подтверждено, что материал опубликован",
  PUBLICATION_RECONCILED_NOT_PUBLISHED: "Подтверждено, что материал не опубликован",
  PUBLICATION_RECONCILIATION_REQUIRED: "Требуется сверить результат отправки",
  PUBLICATION_RECOVERY_REQUIRED: "Требуется восстановить публикацию",
  PUBLICATION_RETRY_SCHEDULED: "Повторная отправка запланирована",
  PUBLICATION_METRICS_SYNC_FAILED: "Не удалось обновить метрики публикации",
  PUBLICATION_PLAN_CREATED: "Создан план публикаций",
  PUBLICATION_PLAN_UPDATED: "План публикаций обновлён",
  PUBLICATION_PLAN_GENERATION_QUEUED: "План поставлен в очередь на подготовку",
  PUBLICATION_PLAN_GENERATION_STARTED: "Началась подготовка плана публикаций",
  PUBLICATION_PLAN_GENERATED: "План публикаций подготовлен",
  PUBLICATION_PLAN_GENERATION_FAILED: "Не удалось подготовить план публикаций",
  PUBLICATION_PLAN_GENERATION_REPAIR_ATTEMPTED: "План публикаций отправлен на исправление",
  PUBLICATION_PLAN_ITEM_USED_FOR_SMM: "Пункт плана использован для создания поста",
  CONTENT_APPROVED: "Материал утверждён",
  CONTENT_REJECTED: "Материал отклонён",
  CONTENT_REVISION_REQUESTED: "Запрошена доработка материала",
  CONTENT_REVISION_COMPLETED: "Доработка материала завершена",
  ARTICLE_CREATED: "Создана статья",
  SOCIAL_POST_PACK_CREATED: "Создан пакет постов",
  KNOWLEDGE_PACK_CREATED: "Подготовлены источники знаний",
  FEEDBACK_ANALYSIS_QUEUED: "Выводы по обратной связи поставлены в очередь",
  FEEDBACK_ANALYSIS_STARTED: "Началась обработка обратной связи",
  FEEDBACK_ANALYSIS_COMPLETED: "Выводы по обратной связи готовы",
  FEEDBACK_ANALYSIS_FAILED: "Не удалось подготовить выводы по обратной связи",
  FEEDBACK_ANALYSIS_USED: "Выводы по обратной связи применены",
  MARKETING_FEEDBACK_CREATED: "Добавлена обратная связь",
  AGENT_RUN_FAILED: "AI-задача завершилась ошибкой",
  CAMPAIGN_STRATEGY_APPROVED: "Стратегия утверждена",
  STRATEGY_APPROVED: "Стратегия утверждена",
  TASK_AUTO_DISPATCHED: "Задача автоматически передана исполнителю",
  TASK_RECOVERED_FROM_STUCK_RUN: "Задача восстановлена после остановки запуска",
  TASK_RETRY_EXHAUSTED: "Исчерпаны попытки выполнения задачи",
  TASK_RETRY_SCHEDULED: "Повтор задачи запланирован",
  SMM_OPERATOR_RECOVERY: "Пост восстановлен оператором",
};

export function activityLabel(event: Activity): string {
  return ACTIVITY_LABELS[event.event_type] ?? event.event_type.replaceAll("_", " ").toLocaleLowerCase("ru-RU");
}

export function contentStatusHelp(status: ContentStatus): string {
  if (status === "WAITING_APPROVAL") return "Материал ждёт решения человека.";
  if (status === "WAITING_REVIEW") return "Материал проходит внутреннюю проверку.";
  if (status === "APPROVED") return "Материал утверждён и может перейти к следующему шагу.";
  if (status === "REJECTED") return "Материал отклонён.";
  if (status === "ARCHIVED") return "Материал доступен только для просмотра.";
  return "Работа над материалом ещё не завершена.";
}
