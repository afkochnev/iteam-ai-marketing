"use client";

import { useEffect, useState, type FormEvent } from "react";
import { FriendlyError } from "@/components/friendly-error";
import { ApiError } from "@/lib/api";
import { studioApi, type StudioWebsitePage, type StudioWebsiteStatus } from "@/lib/studio";

export function StudioWebsiteSandbox({ campaignId, versionId, disabled }: {
  campaignId: string;
  versionId: string;
  disabled: boolean;
}) {
  const [status, setStatus] = useState<StudioWebsiteStatus>();
  const [loadError, setLoadError] = useState<unknown>();
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const draftConflict = error instanceof ApiError && error.code === "WEBSITE_DRAFT_ALREADY_EXISTS";
  const canCreateDraft = !busy && !disabled && !loadingStatus && !draftConflict;

  useEffect(() => {
    const controller = new AbortController();
    void studioApi.websiteStatus(campaignId, versionId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setStatus(result);
      if (result.page) setError(undefined);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setLoadError(reason);
    }).finally(() => {
      if (!controller.signal.aborted) setLoadingStatus(false);
    });
    return () => controller.abort();
  }, [campaignId, versionId, loadAttempt]);

  function reloadStatus() {
    setLoadError(undefined);
    setLoadingStatus(true);
    setLoadAttempt((attempt) => attempt + 1);
  }

  async function save(action: () => Promise<StudioWebsitePage>) {
    setLoadError(undefined);
    setError(undefined);
    setBusy(true);
    try {
      setStatus({ enabled: true, page: await action() });
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  function createDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCreateDraft) return;
    const fields = new FormData(event.currentTarget);
    void save(() => studioApi.createWebsiteDraft(campaignId, versionId, {
      seo_title: String(fields.get("seo_title") ?? ""),
      seo_description: String(fields.get("seo_description") ?? ""),
    }));
  }

  if (!status && loadError != null) {
    return <FriendlyError error={loadError} fallback="Не удалось проверить тестовую CMS." onRetry={reloadStatus} />;
  }
  if (!status?.enabled) return null;
  const page = status.page;

  return (
    <section className="section-block" aria-label="Тестовая CMS">
      <h4>Тестовая CMS</h4>
      {page ? (
        <>
          <p role="status">{page.published_at ? "Опубликовано на тестовом сайте" : "Черновик подготовлен"}</p>
          <p>ID страницы <code>{page.id}</code></p>
          <div className="actions">
            {page.published_at ? (
              <a href={studioApi.websitePublicUrl(page.id)} target="_blank" rel="noopener">Открыть тестовую публикацию</a>
            ) : (
              <>
                <a href={studioApi.websiteDraftUrl(campaignId, versionId, page.id)} target="_blank" rel="noopener">Посмотреть черновик</a>
                <button type="button" disabled={busy || disabled} onClick={() => void save(() => studioApi.publishWebsiteDraft(campaignId, versionId, page.id))}>
                  Опубликовать на тестовом сайте
                </button>
              </>
            )}
          </div>
        </>
      ) : (
        <form onSubmit={createDraft}>
          <label>SEO-заголовок <input name="seo_title" maxLength={120} /></label>
          <label>SEO-описание <textarea name="seo_description" rows={2} maxLength={320} /></label>
          <button type="submit" disabled={!canCreateDraft}>Создать черновик в тестовой CMS</button>
        </form>
      )}
      {draftConflict ? (
        <>
          <p role="alert">Для этой версии уже есть черновик с другими SEO-полями.</p>
          <button type="button" className="secondary" disabled={busy || loadingStatus} onClick={reloadStatus}>
            {loadingStatus ? "Загружаем черновик…" : "Открыть существующий черновик"}
          </button>
          {loadError != null && <FriendlyError error={loadError} fallback="Не удалось загрузить существующий черновик." />}
        </>
      ) : (
        error != null && <FriendlyError error={error} fallback="Не удалось выполнить действие в тестовой CMS." />
      )}
    </section>
  );
}
