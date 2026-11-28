"use client";

import { useEffect, useId, useRef, useState } from "react";
import { FriendlyError } from "@/components/friendly-error";
import { formatDateTime } from "@/lib/campaigns";
import {
  studioApi,
  type StudioWordPressRevisionConfirmation,
  type StudioWordPressRevisionsState,
  type StudioWordPressTarget,
} from "@/lib/studio";

const REFRESH = "Обновить историю публикации";
const REVISION_STATES = {
  queued: "В очереди",
  running: "Выполняется",
  unknown: "Результат требует сверки",
  failed: "Операция остановлена",
  succeeded: "Подтверждено WordPress",
};

function newConfirmationId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

export function StudioWordPressRevisions({
  campaignId, versionId, versionNumber, target, disabled,
}: {
  campaignId: string;
  versionId: string;
  versionNumber: number;
  target: StudioWordPressTarget;
  disabled: boolean;
}) {
  const [status, setStatus] = useState<StudioWordPressRevisionsState>();
  const [selection, setSelection] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<unknown>();
  const [commandError, setCommandError] = useState<unknown>();
  const attempt = useRef<StudioWordPressRevisionConfirmation | null>(null);
  const selectId = useId();
  const error = loadError ?? commandError;
  const writesDisabled = disabled || busy || refreshing || error != null;

  const selected = status?.versions.find((version) => version.id === selection);
  const currentRevision = status?.revisions.find((revision) => revision.id === status.current_revision_id);
  const active = status?.revisions.some((revision) =>
    revision.status === "queued" || revision.status === "running" || revision.status === "unknown",
  );
  const polling = status?.revisions.some((revision) =>
    revision.status === "queued" || revision.status === "running" || revision.reconciliation_pending,
  );

  useEffect(() => {
    if (disabled || busy || (!refreshing && (error != null || (status && !polling)))) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void studioApi.wordpressRevisions(campaignId, versionId, controller.signal).then(
        (result) => {
          if (controller.signal.aborted) return;
          setStatus(result);
          setLoadError(undefined);
          setCommandError(undefined);
          setRefreshing(false);
        },
        (reason: unknown) => {
          if (controller.signal.aborted) return;
          setLoadError(reason);
          setRefreshing(false);
        },
      );
    }, status && !refreshing ? 1500 : 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [campaignId, versionId, disabled, busy, refreshing, error, status, polling]);

  async function confirm() {
    if (!status || !selected || writesDisabled || active) return;
    const previous = attempt.current;
    const confirmation = {
      ...target,
      command_id: previous?.article_version_id === selected.id
        && previous.expected_revision_id === status.current_revision_id
        && previous.mode === selected.mode
        ? previous.command_id : newConfirmationId(),
      expected_revision_id: status.current_revision_id,
      article_version_id: selected.id,
      mode: selected.mode,
    };
    attempt.current = confirmation;
    setBusy(true);
    try {
      setStatus(await studioApi.confirmWordPressRevision(campaignId, versionId, confirmation));
      attempt.current = null;
      setSelection("");
    } catch (reason) {
      setCommandError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function reconcile(revisionId: string) {
    if (writesDisabled) return;
    setBusy(true);
    try {
      setStatus(await studioApi.reconcileWordPressRevision(campaignId, versionId, revisionId, target));
    } catch (reason) {
      setCommandError(reason);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section-block" aria-label="Версии публикации WordPress">
      <h5>Версии публикации WordPress</h5>
      {!status && error == null && !disabled && <p role="status">Загружаем версии публикации…</p>}
      {status && (
        <>
          <p>Текущая версия на WordPress <strong>v{currentRevision?.version_number ?? versionNumber}</strong></p>
          {status.versions.length > 0 ? (
            <div className="stack">
              <label htmlFor={selectId}>Утверждённая версия для публикации</label>
              <select id={selectId} value={selected?.id ?? ""} disabled={writesDisabled || active}
                onChange={(event) => setSelection(event.currentTarget.value)}>
                <option value="">Выберите версию</option>
                {status.versions.map((version) => (
                  <option key={version.id} value={version.id}>v{version.version_number} · {version.title}</option>
                ))}
              </select>
              {selected && (
                <button type="button" disabled={writesDisabled || active} onClick={() => void confirm()}>
                  {selected.mode === "restore"
                    ? `Восстановить версию v${selected.version_number}`
                    : `Подтвердить обновление до v${selected.version_number}`}
                </button>
              )}
            </div>
          ) : <p>Нет утверждённых версий для обновления или восстановления.</p>}
          {status.revisions.length > 0 && (
            <ol className="studio-choice-list" aria-label="История изменений публикации">
              {status.revisions.map((revision) => (
                <li key={revision.id}>
                  <p><strong>{revision.mode === "restore" ? "Восстановление" : "Обновление"} до v{revision.version_number}</strong> · {revision.title}</p>
                  <time dateTime={revision.requested_at}>{formatDateTime(revision.requested_at)}</time>
                  <p role="status">
                    {revision.phase === "image" && "Иллюстрация · "}
                    {revision.reconciliation_pending ? "Сверка запрошена" : REVISION_STATES[revision.status]}
                  </p>
                  {revision.status === "unknown" && (
                    <>
                      {revision.reconciliation_finished_at && !revision.reconciliation_pending && (
                        <p>{revision.reconciliation_failure_code
                          ? "Последняя сверка завершилась ошибкой."
                          : "Последняя сверка не подтвердила результат."}</p>
                      )}
                      <button type="button" className="secondary"
                        disabled={writesDisabled || revision.reconciliation_pending}
                        onClick={() => void reconcile(revision.id)}>
                        Сверить результат для v{revision.version_number}
                      </button>
                    </>
                  )}
                  {(revision.failure_code || revision.reconciliation_failure_code) && (
                    <details>
                      <summary>Технические сведения</summary>
                      {revision.failure_code && <p><code>{revision.failure_code}</code></p>}
                      {revision.reconciliation_failure_code && <p><code>{revision.reconciliation_failure_code}</code></p>}
                    </details>
                  )}
                </li>
              ))}
            </ol>
          )}
        </>
      )}
      {error != null && (
        <FriendlyError error={error} fallback={loadError != null
          ? "Не удалось загрузить версии публикации."
          : "Не удалось получить результат команды. Обновите историю публикации."} />
      )}
      <button type="button" className="secondary" disabled={disabled || busy || refreshing} aria-busy={refreshing}
        onClick={() => setRefreshing(true)}>{REFRESH}</button>
    </section>
  );
}
