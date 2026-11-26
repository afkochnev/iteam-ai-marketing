"use client";

import { useEffect, useState } from "react";
import { FriendlyError } from "@/components/friendly-error";
import {
  studioApi,
  type StudioWordPressAction,
  type StudioWordPressStatus,
} from "@/lib/studio";

const LABELS = {
  draft: "Черновик",
  image: "Иллюстрация",
  publish: "Публикация",
} satisfies Record<StudioWordPressAction, string>;

const STATES = {
  queued: "В очереди",
  running: "Выполняется",
  unknown: "Результат требует сверки",
  failed: "Остановлено до изменения в WordPress",
  succeeded: "Подтверждено WordPress",
};

const PUBLICATION_WAITING_FOR_UPLOAD = "Публикация ожидает загрузки иллюстрации";
const PUBLICATION_BY_IMAGE_STATE = {
  queued: PUBLICATION_WAITING_FOR_UPLOAD,
  running: PUBLICATION_WAITING_FOR_UPLOAD,
  unknown: "Публикация ожидает подтверждения иллюстрации",
  failed: "Публикация остановлена. Иллюстрация не загружена.",
  succeeded: null,
} satisfies Record<keyof typeof STATES, string | null>;

const REFRESH_STATUS = "Обновить статус WordPress";

export function StudioWordPress({ campaignId, versionId, disabled }: {
  campaignId: string;
  versionId: string;
  disabled: boolean;
}) {
  const [status, setStatus] = useState<StudioWordPressStatus>();
  const [loadError, setLoadError] = useState<unknown>();
  const [actionError, setActionError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const statusError = loadError ?? actionError;
  const commandsDisabled = busy || refreshing || disabled || status?.target_changed || statusError != null;

  const draft = status?.operations.find((operation) => operation.action === "draft");
  const image = status?.operations.find((operation) => operation.action === "image");
  const publication = status?.operations.find((operation) => operation.action === "publish");
  const publicationQueueLabel = publication?.status === "queued" && image
    ? PUBLICATION_BY_IMAGE_STATE[image.status] : null;
  const waiting = status?.operations.some((operation) => (
    operation.reconciliation_pending
    || operation.status === "running"
    || (operation.status === "queued" && (
      operation.action !== "publish" || !image || image.status === "succeeded"
    ))
  ));

  useEffect(() => {
    const shouldPoll = statusError == null && (!status || (waiting && !status.target_changed));
    if (busy || (!refreshing && !shouldPoll)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void studioApi.wordpressStatus(campaignId, versionId, controller.signal)
        .then((result) => {
          if (controller.signal.aborted) return;
          setStatus(result);
          setActionError(undefined);
          setRefreshing(false);
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          setLoadError(reason);
          setRefreshing(false);
        });
    }, status && !refreshing ? 1500 : 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [campaignId, versionId, status, waiting, statusError, busy, refreshing]);

  function refreshStatus() {
    setLoadError(undefined);
    setRefreshing(true);
  }

  async function act(action: StudioWordPressAction, reconcile = false) {
    if (!status?.target_url || !status.target_author_id) return;
    setActionError(undefined);
    setBusy(true);
    try {
      setStatus(await studioApi.wordpressCommand(campaignId, versionId, action, {
        target_url: status.target_url,
        target_author_id: status.target_author_id,
      }, reconcile));
    } catch (reason) {
      setActionError(reason);
    } finally {
      setBusy(false);
    }
  }

  if (!status?.target_url) {
    return loadError != null ? <FriendlyError error={loadError} fallback="Не удалось проверить WordPress."
      onRetry={refreshStatus} retryLabel={REFRESH_STATUS} /> : null;
  }

  return (
    <section className="section-block" aria-label="Тестовый WordPress">
      <h4>Тестовый WordPress</h4>
      <p>{status.target_url}</p>
      <p>ID автора WordPress <code>{status.target_author_id}</code></p>
      {status.target_changed && <p role="status">Учётная запись WordPress изменилась.</p>}
      {status.operations.map((operation) => (
        <div key={operation.action}>
          <p role="status">
            {operation.action === "publish" && publicationQueueLabel ? publicationQueueLabel : (
              <>{LABELS[operation.action]} — {
                operation.reconciliation_pending ? "Сверка запрошена" : STATES[operation.status]
              }</>
            )}
          </p>
          {operation.remote_id && <p>ID WordPress <code>{operation.remote_id}</code></p>}
          {operation.remote_url && (
            <a href={operation.remote_url} target="_blank" rel="noopener">Открыть публикацию в WordPress</a>
          )}
          {operation.status === "unknown" && (
            <>
              {operation.reconciliation_finished_at && !operation.reconciliation_pending && (
                <p>Последняя сверка не подтвердила результат.</p>
              )}
              <button type="button"
                disabled={commandsDisabled || operation.reconciliation_pending}
                onClick={() => void act(operation.action, true)}>
                Сверить результат · {LABELS[operation.action]}
              </button>
            </>
          )}
          {operation.status === "failed" && operation.failure_code && (
            <details><summary>Технические сведения</summary><code>{operation.failure_code}</code></details>
          )}
        </div>
      ))}
      {!draft && (
        <button type="button" disabled={commandsDisabled}
          onClick={() => void act("draft")}>Создать черновик в WordPress</button>
      )}
      {status.draft_preview_url && (
        <div>
          <a href={status.draft_preview_url} target="_blank" rel="noopener noreferrer">Посмотреть черновик в WordPress</a>
          <p>Для предпросмотра войдите в WordPress с правом просмотра черновика.</p>
        </div>
      )}
      {draft?.status === "succeeded" && !publication && (
        <button type="button" disabled={commandsDisabled}
          onClick={() => void act("publish")}>Подтвердить публикацию в WordPress</button>
      )}
      {statusError != null && (
        <>
          <FriendlyError error={statusError} fallback={loadError != null
            ? "Не удалось проверить WordPress." : "Не удалось получить ответ на команду WordPress."} />
          {loadError != null && <p>Показан последний полученный статус.</p>}
        </>
      )}
      <button type="button" className="secondary" disabled={busy || refreshing} aria-busy={refreshing}
        onClick={refreshStatus}>{REFRESH_STATUS}</button>
    </section>
  );
}
