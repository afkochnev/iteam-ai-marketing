"use client";

import { useCallback, useState } from "react";
import { FriendlyError } from "@/components/friendly-error";
import { StudioPagination, useStudioPage } from "@/components/studio/pages";
import { studioApi, type StudioAsset, type StudioVersion } from "@/lib/studio";

interface VersionReference {
  id: string;
  label?: string;
}

export function StudioVersionPicker({
  campaignId,
  label,
  value,
  onChange,
  approvedIllustrations = false,
}: {
  campaignId: string;
  label: string;
  value: VersionReference | undefined;
  onChange: (value: VersionReference | undefined) => void;
  approvedIllustrations?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="studio-relation">
      <div className="actions">
        <span><strong>{label}</strong>{value ? ` · ${value.label ?? value.id}` : " · не выбрана"}</span>
        <button type="button" className="secondary" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Скрыть выбор" : `Выбрать · ${label.toLowerCase()}`}
        </button>
        {value && <button type="button" className="secondary" onClick={() => onChange(undefined)}>Убрать связь</button>}
      </div>
      {open && <VersionOptions
        campaignId={campaignId}
        label={label}
        approvedIllustrations={approvedIllustrations}
        onChoose={(version) => {
          onChange({ id: version.id, label: `${version.payload.title} · v${version.version_number}` });
          setOpen(false);
        }}
      />}
    </div>
  );
}

function VersionOptions({
  campaignId,
  label,
  approvedIllustrations,
  onChoose,
}: {
  campaignId: string;
  label: string;
  approvedIllustrations: boolean;
  onChoose: (version: StudioVersion) => void;
}) {
  const [asset, setAsset] = useState<StudioAsset>();
  const read = useCallback(
    (offset: number, signal: AbortSignal) => studioApi.assets(campaignId, offset, signal, approvedIllustrations ? "ILLUSTRATION" : undefined),
    [campaignId, approvedIllustrations],
  );
  const listing = useStudioPage(read);
  return (
    <section className="card studio-options" aria-label={`Выбор · ${label}`}>
      {asset ? (
        <>
          <div className="section-heading">
            <strong>{asset.latest_version.payload.title}</strong>
            <button type="button" className="secondary" onClick={() => setAsset(undefined)}>К списку материалов</button>
          </div>
          <VersionChoices
            key={asset.id}
            campaignId={campaignId}
            assetId={asset.id}
            approvedOnly={approvedIllustrations}
            onChoose={onChoose}
          />
        </>
      ) : (
        <>
          {listing.loading && <p role="status">Загружаем материалы…</p>}
          {listing.error != null && <FriendlyError error={listing.error} fallback="Не удалось загрузить список материалов." onRetry={() => listing.refresh()} />}
          {listing.page && (
            <>
              {listing.page.items.length === 0 ? <p>В кампании пока нет подходящих материалов.</p> : (
                <ul className="studio-choice-list">
                  {listing.page.items.map((candidate) => (
                    <li key={candidate.id}>
                      <button type="button" className="secondary" onClick={() => setAsset(candidate)}>
                        {candidate.latest_version.payload.title} · {candidate.kind === "ARTICLE" ? "статья" : "иллюстрация"}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <StudioPagination page={listing.page} noun="материалы для связи" onChange={listing.setOffset} />
            </>
          )}
        </>
      )}
    </section>
  );
}

function VersionChoices({
  campaignId,
  assetId,
  approvedOnly,
  onChoose,
}: {
  campaignId: string;
  assetId: string;
  approvedOnly: boolean;
  onChoose: (version: StudioVersion) => void;
}) {
  const read = useCallback(
    (offset: number, signal: AbortSignal) => studioApi.versions(campaignId, assetId, offset, signal),
    [campaignId, assetId],
  );
  const listing = useStudioPage(read);
  if (listing.loading) return <p role="status">Загружаем версии…</p>;
  if (!listing.page) return <FriendlyError error={listing.error} fallback="Не удалось загрузить версии." onRetry={() => listing.refresh()} />;
  const choices = listing.page.items.filter((version) => !approvedOnly || version.kind === "ILLUSTRATION" && version.approval !== null);
  return (
    <>
      {choices.length === 0 ? (
        <p>{approvedOnly ? "На этой странице нет утверждённых версий." : "На этой странице нет версий."}</p>
      ) : (
        <ul className="studio-choice-list">
          {choices.map((version) => (
            <li key={version.id}>
              <button type="button" className="secondary" onClick={() => onChoose(version)}>
                Выбрать v{version.version_number} · {version.approval ? "утверждена" : "черновик"}
              </button>
            </li>
          ))}
        </ul>
      )}
      <StudioPagination page={listing.page} noun="версии для связи" onChange={listing.setOffset} />
    </>
  );
}
