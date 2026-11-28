"use client";

import { useEffect, useId, useState } from "react";
import { FriendlyError } from "@/components/friendly-error";
import { StudioWordPressRevisions } from "@/components/studio/wordpress-revisions";
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

const SCHEDULED_LABELS: Partial<Record<StudioWordPressAction, string>> = {
  image: "Иллюстрация будет загружена при публикации",
  publish: "Публикация запланирована",
};

const REFRESH_STATUS = "Обновить статус WordPress";
const STATUS_POLL_INTERVAL = 1500;
const MAX_SCHEDULE_POLL_INTERVAL = 60 * 60 * 1000;
const SCHEDULE_FORMAT = new Intl.DateTimeFormat("ru-RU", {
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZoneName: "longOffset",
});
const SCHEDULE_TIME_ZONE = SCHEDULE_FORMAT.resolvedOptions().timeZone;

function readLocalSchedule(value: string): Date | null {
  if (value.length !== 16) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16) === value ? date : null;
}

export function StudioWordPress({ campaignId, versionId, versionNumber, disabled }: {
  campaignId: string;
  versionId: string;
  versionNumber: number;
  disabled: boolean;
}) {
  const [status, setStatus] = useState<StudioWordPressStatus>();
  const [loadError, setLoadError] = useState<unknown>();
  const [actionError, setActionError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [schedule, setSchedule] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const scheduleInputId = useId();
  const scheduleHelpId = useId();
  const statusError = loadError ?? actionError;
  const commandsDisabled = busy || refreshing || disabled || status?.target_changed || statusError != null;

  const draft = status?.operations.find((operation) => operation.action === "draft");
  const image = status?.operations.find((operation) => operation.action === "image");
  const publication = status?.operations.find((operation) => operation.action === "publish");
  const scheduledFor = publication?.scheduled_for;
  const scheduledAt = scheduledFor ? Date.parse(scheduledFor) : Number.NaN;
  const waitingForSchedule = publication?.status === "queued" && scheduledAt > now;
  const publicationQueueLabel = publication?.status === "queued" && image
    ? PUBLICATION_BY_IMAGE_STATE[image.status] : null;
  const waiting = status?.operations.some((operation) => (
    operation.reconciliation_pending
    || operation.status === "running"
    || (operation.status === "queued" && (
      operation.action === "draft"
      || (!waitingForSchedule && (operation.action !== "publish" || !image || image.status === "succeeded"))
    ))
  ));
  const schedulePollDelay = waitingForSchedule
    ? Math.min(scheduledAt - now, MAX_SCHEDULE_POLL_INTERVAL)
    : null;
  const pollDelay = waiting ? STATUS_POLL_INTERVAL : schedulePollDelay;
  const chosenSchedule = readLocalSchedule(schedule);
  const scheduleInvalid = Boolean(schedule) && (!chosenSchedule || chosenSchedule.getTime() <= now);

  useEffect(() => {
    const shouldPoll = statusError == null && (!status || (pollDelay !== null && !status.target_changed));
    if (busy || (!refreshing && !shouldPoll)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void studioApi.wordpressStatus(campaignId, versionId, controller.signal)
        .then((result) => {
          if (controller.signal.aborted) return;
          setStatus(result);
          setNow(Date.now());
          setActionError(undefined);
          setRefreshing(false);
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          setLoadError(reason);
          setRefreshing(false);
        });
    }, status && !refreshing ? (pollDelay ?? 0) : 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [campaignId, versionId, status, pollDelay, statusError, busy, refreshing]);

  function refreshStatus() {
    setLoadError(undefined);
    setRefreshing(true);
  }

  async function act(action: StudioWordPressAction, reconcile = false, scheduledFor?: string) {
    if (!status?.target_url || !status.target_author_id) return;
    setActionError(undefined);
    setBusy(true);
    try {
      setStatus(await studioApi.wordpressCommand(campaignId, versionId, action, {
        target_url: status.target_url,
        target_author_id: status.target_author_id,
        ...(scheduledFor ? { scheduled_for: scheduledFor } : {}),
      }, reconcile));
    } catch (reason) {
      setActionError(reason);
    } finally {
      setNow(Date.now());
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
      {status.operations.map((operation) => {
        const label = (waitingForSchedule && operation.status === "queued" ? SCHEDULED_LABELS[operation.action] : null)
          ?? (operation.action === "publish" ? publicationQueueLabel : null)
          ?? `${LABELS[operation.action]} — ${operation.reconciliation_pending ? "Сверка запрошена" : STATES[operation.status]}`;
        return (
          <div key={operation.action}>
            <p role="status">{label}</p>
            {operation.action === "publish" && scheduledFor && Number.isFinite(scheduledAt) && (
              <p>Запланированное время <time dateTime={scheduledFor}>
                {SCHEDULE_FORMAT.format(scheduledAt)}
              </time></p>
            )}
            {operation.remote_id && <p>ID WordPress <code>{operation.remote_id}</code></p>}
            {operation.remote_url && (
              <a href={operation.remote_url} target="_blank" rel="noopener">Открыть публикацию в WordPress</a>
            )}
            {operation.status === "unknown" && (
              <>
                {operation.reconciliation_finished_at && !operation.reconciliation_pending && (
                  <>
                    <p role="status">{operation.reconciliation_failure_code
                      ? "Последняя сверка завершилась ошибкой."
                      : "Последняя сверка не подтвердила результат."}</p>
                    {operation.reconciliation_failure_code && (
                      <details>
                        <summary>Технические сведения о сверке</summary>
                        <code>{operation.reconciliation_failure_code}</code>
                      </details>
                    )}
                  </>
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
        );
      })}
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
        <div className="stack">
          <button type="button" disabled={commandsDisabled}
            onClick={() => void act("publish")}>Подтвердить публикацию в WordPress</button>
          <div className="stack">
            <label htmlFor={scheduleInputId}>Дата и время публикации</label>
            <input id={scheduleInputId} type="datetime-local" value={schedule}
              disabled={commandsDisabled} aria-invalid={scheduleInvalid}
              aria-describedby={scheduleHelpId}
              onChange={(event) => {
                setSchedule(event.currentTarget.value);
                setNow(Date.now());
              }} />
            <p id={scheduleHelpId}>
              Часовой пояс браузера <code>{SCHEDULE_TIME_ZONE}</code>.
              {chosenSchedule && <> Выбранное время {SCHEDULE_FORMAT.format(chosenSchedule)}.</>}
              {scheduleInvalid && " Укажите допустимые дату и время в будущем."}
            </p>
            <button type="button" disabled={commandsDisabled || !chosenSchedule || scheduleInvalid}
              onClick={() => {
                const currentTime = Date.now();
                setNow(currentTime);
                if (chosenSchedule && chosenSchedule.getTime() > currentTime) {
                  void act("publish", false, chosenSchedule.toISOString());
                }
              }}>
              Запланировать публикацию в WordPress
            </button>
          </div>
        </div>
      )}
      {publication?.status === "succeeded" && publication.remote_url && status.target_author_id && !status.target_changed && (
        <StudioWordPressRevisions
          key={`${status.target_url}:${status.target_author_id}`}
          campaignId={campaignId}
          versionId={versionId}
          versionNumber={versionNumber}
          target={{ target_url: status.target_url, target_author_id: status.target_author_id }}
          disabled={Boolean(commandsDisabled)}
        />
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
