import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { presentError, safeAnalysisValidationCategory } from "@/lib/user-errors";

describe("user-facing error presentation", () => {
  it("explains a missing approved article without exposing only a code", () => {
    const view = presentError(new ApiError("No approved articles", 409, "APPROVED_ARTICLES_NOT_FOUND"));
    expect(view.title).toBe("Не удалось создать медиаплан");
    expect(view.reason).toContain("утверждённых статей");
    expect(view.nextStep).toContain("утвердите");
    expect(view.retryable).toBe(false);
    expect(view.code).toBe("APPROVED_ARTICLES_NOT_FOUND");
  });

  it("keeps unknown technical information available behind the details disclosure", () => {
    const view = presentError(new ApiError("upstream queue timeout", 503, "UNEXPECTED_QUEUE_ERROR"));
    expect(view.title).toBe("Не удалось выполнить действие.");
    expect(view.technicalMessage).toBe("upstream queue timeout");
    expect(view.code).toBe("UNEXPECTED_QUEUE_ERROR");
  });

  it("explains network errors in user language", () => {
    const view = presentError(new TypeError("Failed to fetch"), "Не удалось открыть задачу");
    expect(view.reason).toContain("связаться с API");
    expect(view.retryable).toBe(true);
  });

  it("explains a blocked plan-item run without exposing only 409", () => {
    const view = presentError(new ApiError("Запустить можно только готовую задачу.", 409, "TASK_NOT_READY"));
    expect(view.title).toBe("Пост пока нельзя создать");
    expect(view.reason).toContain("условия запуска");
    expect(view.nextStep).toContain("пункта медиаплана");
    expect(view.retryable).toBe(false);
  });
});

it("analyst repair exhaustion gives safe guidance without repeating raw provider text", () => {
  const view = presentError(new ApiError("PRIVATE_FEEDBACK_SECRET_123", 422, "FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED"));
  expect(view.title).toBe("Анализ не прошёл проверку");
  expect(view.reason).toContain("дважды");
  expect(view.nextStep).toContain("Не повторяйте анализ многократно");
  expect(view.retryable).toBe(false);
  expect(JSON.stringify(view)).not.toContain("PRIVATE_FEEDBACK_SECRET_123");
});

it("technical analyst category accepts only application codes", () => {
  expect(safeAnalysisValidationCategory("OPTIMIZATION_TARGET_INVALID")).toBe("OPTIMIZATION_TARGET_INVALID");
  expect(safeAnalysisValidationCategory("PRIVATE_FEEDBACK_SECRET_123")).toBeUndefined();
  expect(safeAnalysisValidationCategory({code:"SCHEMA_INVALID"})).toBeUndefined();
});
