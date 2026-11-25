"use client";

import { useEffect, useState, type FormEvent } from "react";
import { FriendlyError } from "@/components/friendly-error";
import { studioApi, type StudioWebsitePage, type StudioWebsiteStatus } from "@/lib/studio";

export function StudioWebsiteSandbox({ campaignId, versionId, disabled }: {
  campaignId: string;
  versionId: string;
  disabled: boolean;
}) {
  const [status, setStatus] = useState<StudioWebsiteStatus>();
  const [loadError, setLoadError] = useState<unknown>();
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void studioApi.websiteStatus(campaignId, versionId, controller.signal).then((result) => {
      if (!controller.signal.aborted) setStatus(result);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setLoadError(reason);
    });
    return () => controller.abort();
  }, [campaignId, versionId, loadAttempt]);

  async function save(action: () => Promise<StudioWebsitePage>) {
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
    const fields = new FormData(event.currentTarget);
    void save(() => studioApi.createWebsiteDraft(campaignId, versionId, {
      seo_title: String(fields.get("seo_title") ?? ""),
      seo_description: String(fields.get("seo_description") ?? ""),
    }));
  }

  if (loadError != null) {
    return <FriendlyError error={loadError} fallback="Не удалось проверить тестовую CMS." onRetry={() => {
      setLoadError(undefined);
      setLoadAttempt((attempt) => attempt + 1);
    }} />;
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
          <button type="submit" disabled={busy || disabled}>Создать черновик в тестовой CMS</button>
        </form>
      )}
      {error != null && <FriendlyError error={error} fallback="Не удалось выполнить действие в тестовой CMS." />}
    </section>
  );
}
