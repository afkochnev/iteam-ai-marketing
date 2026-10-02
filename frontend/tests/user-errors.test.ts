import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { presentError } from "@/lib/user-errors";

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
