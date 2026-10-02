import { ApiError } from "@/lib/api";

export interface UserErrorPresentation {
  title: string;
  reason: string;
  nextStep: string;
  retryable: boolean;
  code?: string;
  technicalMessage?: string;
}

const ERROR_HELP: Record<string, Omit<UserErrorPresentation, "code" | "technicalMessage">> = {
  APPROVED_ARTICLES_NOT_FOUND: {
    title: "Не удалось создать медиаплан",
    reason: "В кампании пока нет утверждённых статей.",
    nextStep: "Откройте материалы кампании и утвердите хотя бы одну статью.",
    retryable: false,
  },
  PLAN_ENQUEUE_FAILED: {
    title: "Медиаплан не поставлен в работу",
    reason: "Очередь не приняла задачу на генерацию.",
    nextStep: "Проверьте доступность runtime и повторите действие позже.",
    retryable: true,
  },
  PUBLICATION_PLAN_SOURCE_INVALID: {
    title: "Пост не создан",
    reason: "В медиаплане выбрана версия статьи, которая не совпадает с утверждённой.",
    nextStep: "Проверьте источник пункта медиаплана и его утверждённую версию.",
    retryable: false,
  },
  WORKER_INTERRUPTED: {
    title: "Выполнение остановилось",
    reason: "Задача прервалась до завершения.",
    nextStep: "Откройте задачу, проверьте состояние и доступность повтора.",
    retryable: true,
  },
  QUEUE_ENQUEUE_FAILED: {
    title: "Задача не попала в очередь",
    reason: "Runtime не подтвердил постановку задачи.",
    nextStep: "Проверьте состояние очереди и повторите запуск, если он доступен.",
    retryable: true,
  },
  TASK_RETRY_EXHAUSTED: {
    title: "Попытки выполнения исчерпаны",
    reason: "Автоматические повторы больше не запланированы.",
    nextStep: "Разберите технические сведения задачи и устраните первопричину.",
    retryable: false,
  },
  AGENT_INACTIVE: {
    title: "Исполнитель недоступен",
    reason: "Назначенный агент не активен.",
    nextStep: "Попросите администратора проверить назначение и статус агента.",
    retryable: false,
  },
  REQUIRED_AGENT_TOOL_UNAVAILABLE: {
    title: "Не хватает инструмента для задачи",
    reason: "Назначенному агенту недоступен обязательный инструмент.",
    nextStep: "Попросите администратора проверить настройки роли агента.",
    retryable: false,
  },
};

export function presentError(error: unknown, fallback = "Не удалось выполнить действие."): UserErrorPresentation {
  const message = error instanceof Error ? error.message : "";
  const code = error instanceof ApiError ? error.code : undefined;
  const mapped = code ? ERROR_HELP[code] : undefined;
  if (mapped) return { ...mapped, code, technicalMessage: message || undefined };
  if (error instanceof TypeError && /fetch|network/i.test(message)) {
    return {
      title: fallback,
      reason: "Не удалось связаться с API приложения. Проверьте соединение с сервером.",
      nextStep: "Проверьте соединение с приложением и повторите попытку.",
      retryable: true,
      technicalMessage: message || undefined,
    };
  }
  if (error instanceof ApiError && error.status === 403) {
    return {
      title: fallback,
      reason: "У вашей роли нет доступа к этому действию.",
      nextStep: "Попросите администратора проверить права доступа.",
      retryable: false,
      code,
      technicalMessage: message || undefined,
    };
  }
  return {
    title: fallback,
    reason: "Система не смогла завершить действие.",
    nextStep: "Проверьте технические сведения и попробуйте ещё раз, если причина устранена.",
    retryable: true,
    code,
    technicalMessage: message || undefined,
  };
}
