"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FriendlyError } from "@/components/friendly-error";
import { StatusBadge } from "@/components/status-badge";
import { StudioPagination, useStudioPage } from "@/components/studio/pages";
import { StudioWebsiteSandbox } from "@/components/studio/website-sandbox";
import { formatDateTime } from "@/lib/campaigns";
import { studioApi, type StudioAsset, type StudioMedia, type StudioVersion } from "@/lib/studio";

export function VersionBadge({ version }: { version: StudioVersion }) {
  return <StatusBadge label={version.approval ? "Утверждена" : "Черновик"} status={version.approval ? "APPROVED" : "DRAFT"} />;
}

export function StudioHistory({
  campaignId,
  asset,
  selectedVersionId,
  refreshCount,
  disabled,
  onSelect,
}: {
  campaignId: string;
  asset: StudioAsset;
  selectedVersionId: string;
  refreshCount: number;
  disabled: boolean;
  onSelect: (version: StudioVersion) => void;
}) {
  const read = useCallback(
    (offset: number, signal: AbortSignal) => studioApi.versions(campaignId, asset.id, offset, signal),
    [campaignId, asset.id],
  );
  const versions = useStudioPage(read, refreshCount);

  return (
    <section className="section-block" aria-label="История версий">
      <h3>История версий</h3>
      {versions.loading && <p role="status">Загружаем историю…</p>}
      {versions.error != null && <FriendlyError error={versions.error} fallback="Не удалось загрузить историю." onRetry={() => versions.refresh()} />}
      {versions.page && (
        <>
          <ul className="studio-choice-list">
            {versions.page.items.map((version) => (
              <li key={version.id} className="actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={disabled}
                  aria-pressed={selectedVersionId === version.id}
                  onClick={() => onSelect(version)}
                >
                  Смотреть v{version.version_number}
                </button>
                <VersionBadge version={version} />
                {version.id === asset.latest_version.id && <span>Последняя версия</span>}
                <time dateTime={version.created_at}>{formatDateTime(version.created_at)}</time>
              </li>
            ))}
          </ul>
          <StudioPagination page={versions.page} noun="история версий" onChange={versions.setOffset} />
        </>
      )}
    </section>
  );
}

export function StudioVersionDetails({
  campaignId,
  version,
  busy,
  onApprove,
  allowWebsiteDownload = false,
}: {
  campaignId: string;
  version: StudioVersion;
  busy: boolean;
  onApprove?: () => void;
  allowWebsiteDownload?: boolean;
}) {
  return (
    <section className="section-block" aria-label="Выбранная версия">
      <header className="section-heading">
        <h3>{version.payload.title} · v{version.version_number}</h3>
        <VersionBadge version={version} />
      </header>
      {version.kind === "ARTICLE" ? (
        <article className="card studio-article-text">{version.payload.body}</article>
      ) : (
        <IllustrationDetails key={version.id} campaignId={campaignId} version={version} />
      )}
      {version.approval && <p>Согласовано <time dateTime={version.approval.approved_at}>{formatDateTime(version.approval.approved_at)}</time></p>}
      <details>
        <summary>Идентификаторы и связи версии</summary>
        <dl className="studio-metadata">
          <dt>ID версии</dt><dd><code>{version.id}</code></dd>
          {version.supersedes_version_id && <><dt>Предыдущая версия</dt><dd><code>{version.supersedes_version_id}</code></dd></>}
          {version.source_version_id && <><dt>Исходная версия</dt><dd><code>{version.source_version_id}</code></dd></>}
          {version.illustration_version_id && <><dt>Версия иллюстрации</dt><dd><code>{version.illustration_version_id}</code></dd></>}
          {version.approval && <><dt>Согласовал</dt><dd><code>{version.approval.approved_by}</code></dd></>}
          <dt>Создал версию</dt><dd><code>{version.created_by}</code></dd>
          <dt>Хеш снимка</dt><dd><code>{version.snapshot_hash}</code></dd>
        </dl>
      </details>
      {!version.approval && onApprove && (
        <div className="actions studio-approve">
          <button type="button" disabled={busy} onClick={onApprove}>Утвердить версию v{version.version_number}</button>
        </div>
      )}
      {allowWebsiteDownload && version.kind === "ARTICLE" && version.approval && (
        <>
          <WebsitePackageDownload key={version.id} campaignId={campaignId} versionId={version.id} disabled={busy} />
          <StudioWebsiteSandbox key={`site-${version.id}`} campaignId={campaignId} versionId={version.id} disabled={busy} />
        </>
      )}
    </section>
  );
}

function WebsitePackageDownload({ campaignId, versionId, disabled }: {
  campaignId: string;
  versionId: string;
  disabled: boolean;
}) {
  const downloadRequest = useRef<AbortController | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => () => downloadRequest.current?.abort(), []);

  async function download() {
    if (downloadRequest.current) return;
    const controller = new AbortController();
    downloadRequest.current = controller;
    setError(null);
    setDownloading(true);
    try {
      const file = await studioApi.websitePackage(campaignId, versionId, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(file);
      const link = document.createElement("a");
      try {
        link.href = url;
        link.download = `article-${versionId}.zip`;
        document.body.appendChild(link);
        link.click();
      } finally {
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason);
    } finally {
      downloadRequest.current = null;
      if (!controller.signal.aborted) setDownloading(false);
    }
  }

  return (
    <div>
      <div className="actions">
        <button type="button" className="secondary" disabled={disabled || downloading} onClick={() => void download()}>
          {downloading ? "Готовим пакет…" : "Скачать пакет для сайта"}
        </button>
      </div>
      {error != null && <FriendlyError error={error} fallback="Не удалось скачать пакет для сайта." />}
    </div>
  );
}

type IllustrationVersion = Extract<StudioVersion, { kind: "ILLUSTRATION" }>;

function IllustrationDetails({ campaignId, version }: { campaignId: string; version: IllustrationVersion }) {
  const [name, setName] = useState("original");
  const { original, variants, provenance, alt_text, caption, brief, brand_profile, usage_rights } = version.payload;
  const media = variants.find((variant) => variant.name === name) ?? original;

  return (
    <>
      <label>Вариант предпросмотра
        <select value={name} onChange={(event) => setName(event.target.value)}>
          <option value="original">Оригинал · {original.width} × {original.height}</option>
          {variants.map((variant) => (
            <option key={variant.name} value={variant.name}>{variant.name} · {variant.width} × {variant.height}</option>
          ))}
        </select>
      </label>
      <IllustrationPreview key={name} campaignId={campaignId} versionId={version.id} name={name} media={media} alt={alt_text} caption={caption} />
      <dl className="studio-metadata">
        <dt>Происхождение</dt><dd>{provenance.source === "GENERATED" ? "Генерация по брифу" : "Загружено редактором"}</dd>
        <dt>Права использования</dt><dd>{usage_rights}</dd>
        <dt>Альтернативный текст</dt><dd>{alt_text}</dd>
        <dt>Бриф</dt><dd>{brief}</dd>
        <dt>Профиль бренда</dt><dd>{brand_profile}</dd>
        {provenance.model && <><dt>Модель</dt><dd>{provenance.model}</dd></>}
        {provenance.prompt && <><dt>Промпт</dt><dd>{provenance.prompt}</dd></>}
        {provenance.provider_request_id && <><dt>Идентификатор генерации</dt><dd><code>{provenance.provider_request_id}</code></dd></>}
        {provenance.generation_size && <><dt>Размер генерации</dt><dd>{provenance.generation_size}</dd></>}
      </dl>
    </>
  );
}

function IllustrationPreview({
  campaignId,
  versionId,
  name,
  media,
  alt,
  caption,
}: {
  campaignId: string;
  versionId: string;
  name: string;
  media: StudioMedia;
  alt: string;
  caption: string | null;
}) {
  const [result, setResult] = useState<{ url: string } | { error: unknown }>();
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | undefined;
    void studioApi.media(campaignId, versionId, name, controller.signal).then((blob) => {
      if (controller.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob);
      setResult({ url: objectUrl });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setResult({ error });
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [campaignId, versionId, name, attempt]);

  if (!result) return <p role="status">Загружаем предпросмотр…</p>;
  if ("error" in result) return <FriendlyError error={result.error} fallback="Не удалось открыть предпросмотр." onRetry={() => {
    setResult(undefined);
    setAttempt((previous) => previous + 1);
  }} />;

  return (
    <figure className="studio-preview">
      {/* A private object URL is produced by a credentialed request and cannot be optimized by Next. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={result.url} alt={alt} width={media.width} height={media.height} />
      {caption && <figcaption>{caption}</figcaption>}
    </figure>
  );
}
