"use client";

import { presentError } from "@/lib/user-errors";

export function FriendlyError({
  error,
  fallback,
  onRetry,
  retryLabel = "Повторить",
  className = "",
}: {
  error: unknown;
  fallback?: string;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}) {
  const view = presentError(error, fallback);
  return (
    <aside className={`friendly-error ${className}`.trim()} role="alert">
      <h3>{view.title}</h3>
      <p><strong>Почему:</strong> {view.reason}</p>
      <p><strong>Что сделать:</strong> {view.nextStep}</p>
      <div className="actions">
        {view.retryable && onRetry && <button className="secondary" onClick={onRetry}>{retryLabel}</button>}
        {(view.code || view.technicalMessage) && <details>
          <summary>Технические сведения</summary>
          {view.code && <p>Код: <code>{view.code}</code></p>}
          {view.technicalMessage && <p>{view.technicalMessage}</p>}
        </details>}
      </div>
    </aside>
  );
}
