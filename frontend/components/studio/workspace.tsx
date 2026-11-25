"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FriendlyError } from "@/components/friendly-error";
import { ArticleEditor, IllustrationEditor, type IllustrationChange } from "@/components/studio/editors";
import { useStudioGeneration } from "@/components/studio/generation";
import { StudioPagination, useStudioPage } from "@/components/studio/pages";
import { StudioHistory, StudioVersionDetails, VersionBadge } from "@/components/studio/version-details";
import { ApiError } from "@/lib/api";
import {
  studioApi,
  type StudioArticleInput,
  type StudioAsset,
  type StudioIllustrationInput,
  type StudioRevision,
  type StudioVersion,
} from "@/lib/studio";

type IllustrationVersion = Extract<StudioVersion, { kind: "ILLUSTRATION" }>;
type Editor =
  | { kind: "ARTICLE"; previous?: Extract<StudioVersion, { kind: "ARTICLE" }> }
  | { kind: "ILLUSTRATION"; previous?: IllustrationVersion; draft?: StudioIllustrationInput };

interface GenerationDraft {
  jobId: string;
  input: StudioIllustrationInput;
  previous?: IllustrationVersion;
}

const VERSION_CONFLICT = "STUDIO_VERSION_CONFLICT";
const RETRY_VERSION_LOOKUP_LABEL = "Повторить загрузку актуальной версии";

interface Selection {
  asset: StudioAsset;
  version?: StudioVersion;
}

function asRevision(previous?: StudioVersion): StudioRevision | undefined {
  return previous ? { asset_id: previous.asset_id, expected_current_version_id: previous.id } : undefined;
}

function isVersionConflict(reason: unknown) {
  return reason instanceof ApiError && reason.code === VERSION_CONFLICT;
}

export function StudioWorkspace({ campaignId }: { campaignId: string }) {
  const [selection, setSelection] = useState<Selection>();
  const [editor, setEditor] = useState<Editor>();
  const [generationDraft, setGenerationDraft] = useState<GenerationDraft>();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [versionLookupFailure, setVersionLookupFailure] = useState<{ authenticationError?: ApiError }>();
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const resultRequest = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => resultRequest.current?.abort(), []);

  const read = useCallback(
    (offset: number, signal: AbortSignal) => studioApi.assets(campaignId, offset, signal),
    [campaignId],
  );
  const listing = useStudioPage(read);
  const refreshAssets = listing.refresh;
  const refresh = useCallback((firstPage = false) => {
    refreshAssets(firstPage);
    setHistoryRefresh((count) => count + 1);
  }, [refreshAssets]);
  const onGenerationSucceeded = useCallback(() => refresh(true), [refresh]);
  const generation = useStudioGeneration(campaignId, onGenerationSucceeded);

  const selectedAsset = selection?.asset;
  const assets = listing.page?.items.map((listed) => {
    if (listed.id !== selectedAsset?.id) return listed;
    const selected = selectedAsset.latest_version;
    const latest = listed.latest_version;
    return selected.version_number > latest.version_number || (selected.id === latest.id && selected.approval)
      ? selectedAsset
      : listed;
  });
  const asset = assets?.find((candidate) => candidate.id === selectedAsset?.id) ?? selectedAsset;
  const version = selection?.version ?? asset?.latest_version;
  const versionConflict = isVersionConflict(error);
  const conflictingRevision = versionConflict ? editor?.previous : undefined;
  const failedGeneration = generation.job?.status === "FAILED" || generation.job?.status === "UNKNOWN"
    ? generation.job
    : undefined;
  const recovery = failedGeneration?.recovery;
  const recoveredBase = recovery?.previous;
  const localDraft = failedGeneration && failedGeneration.id === generationDraft?.jobId && recovery === undefined
    ? generationDraft
    : undefined;
  const recoverableDraft = recovery && (!recoveredBase || recoveredBase.kind === "ILLUSTRATION")
    ? {
      input: {
        ...recovery.draft,
        caption: recovery.draft.caption ?? undefined,
        source_version_id: recovery.draft.source_version_id ?? undefined,
      },
      previous: recoveredBase ?? undefined,
    }
    : localDraft;

  function openEditor(next?: Editor) {
    setEditor(next);
    setNotice("");
    setError(null);
    setVersionLookupFailure(undefined);
  }

  function startRevision(current: StudioVersion) {
    if (current.kind === "ARTICLE") openEditor({ kind: current.kind, previous: current });
    else openEditor({ kind: current.kind, previous: current });
  }

  function showNewVersion(saved: StudioVersion) {
    setSelection({ asset: { id: saved.asset_id, kind: saved.kind, latest_version: saved }, version: saved });
    setEditor(undefined);
    setNotice(`Сохранён черновик v${saved.version_number}.`);
    refresh(true);
  }

  async function loadLatestVersion(previous: StudioVersion) {
    setVersionLookupFailure(undefined);
    setNotice("");
    const controller = new AbortController();
    resultRequest.current = controller;
    try {
      const page = await studioApi.versions(campaignId, previous.asset_id, 0, controller.signal);
      if (controller.signal.aborted) return;
      const latest = page.items[0];
      if (!latest) throw new Error("Не найдена актуальная версия материала.");
      setSelection((selected) => selected && selected.asset.id === latest.asset_id ? {
        asset: { ...selected.asset, latest_version: latest },
      } : selected);
      refresh();
      setNotice(`Доступна версия v${latest.version_number}. Скопируйте нужные правки, затем закройте редактор и откройте новую редакцию.`);
    } catch (reason) {
      if (controller.signal.aborted) return;
      const authenticationError = reason instanceof ApiError && reason.status === 401 ? reason : undefined;
      setVersionLookupFailure({ authenticationError });
    }
  }

  async function handleSaveError(reason: unknown, previous?: StudioVersion) {
    setError(reason);
    if (previous && isVersionConflict(reason)) await loadLatestVersion(previous);
  }

  async function retryVersionLookup() {
    if (busy || !conflictingRevision) return;
    setBusy(true);
    try {
      await loadLatestVersion(conflictingRevision);
    } finally {
      setBusy(false);
    }
  }

  async function saveArticle(input: StudioArticleInput) {
    setBusy(true);
    setError(null);
    setNotice("");
    const previous = editor?.previous;
    try {
      showNewVersion(await studioApi.saveArticle(campaignId, input, asRevision(previous)));
    } catch (reason) {
      await handleSaveError(reason, previous);
    } finally {
      setBusy(false);
    }
  }

  async function saveIllustration(input: StudioIllustrationInput, change: IllustrationChange) {
    setBusy(true);
    setError(null);
    setNotice("");
    const previous = editor?.kind === "ILLUSTRATION" ? editor.previous : undefined;
    const revision = asRevision(previous);
    try {
      if (change.method !== "generate") {
        const file = change.method === "upload" ? change.file : undefined;
        showNewVersion(await studioApi.uploadIllustration(campaignId, input, file, revision));
      } else {
        const job = await studioApi.generate(campaignId, input, revision);
        setGenerationDraft(job.status === "SUCCEEDED" ? undefined : { jobId: job.id, input, previous });
        generation.update(job);
        if (job.status !== "FAILED" && job.status !== "UNKNOWN") setEditor(undefined);
      }
    } catch (reason) {
      await handleSaveError(reason, previous);
    } finally {
      setBusy(false);
    }
  }

  async function reopenGenerationDraft() {
    if (!recoverableDraft) return;

    const { input, previous } = recoverableDraft;
    openEditor({ kind: "ILLUSTRATION", previous, draft: input });
    if (!previous || failedGeneration?.error_code !== VERSION_CONFLICT) return;

    setSelection((current) => current?.asset.id === previous.asset_id ? current : {
      asset: { id: previous.asset_id, kind: previous.kind, latest_version: previous },
    });
    setBusy(true);
    try {
      await handleSaveError(new ApiError("Материал изменился.", 409, VERSION_CONFLICT), previous);
    } finally {
      setBusy(false);
    }
  }

  function dismissGeneration() {
    setGenerationDraft(undefined);
    generation.dismiss();
  }

  async function approve(shown: StudioVersion) {
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      const updated = await studioApi.approve(campaignId, shown.id);
      setSelection((current) => {
        if (!current || current.asset.id !== updated.asset_id) return current;
        const currentAsset = asset ?? current.asset;
        return {
          asset: currentAsset.latest_version.id === updated.id ? { ...currentAsset, latest_version: updated } : currentAsset,
          version: updated,
        };
      });
      setNotice(`Версия v${updated.version_number} утверждена.`);
      refresh();
    } catch (reason) {
      setError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function openGeneratedVersion() {
    const { asset_id: assetId, version_id: versionId } = generation.job ?? {};
    if (!assetId || !versionId) return;
    setBusy(true);
    setError(null);
    setNotice("");
    const controller = new AbortController();
    resultRequest.current = controller;
    try {
      const [generated, page] = await Promise.all([
        studioApi.version(campaignId, versionId, controller.signal),
        studioApi.versions(campaignId, assetId, 0, controller.signal, 1),
      ]);
      if (controller.signal.aborted) return;
      const latest = page.items[0];
      if (!latest || generated.id !== versionId || generated.asset_id !== assetId) {
        throw new Error("Сохранённая версия задачи не найдена.");
      }
      setSelection({ asset: { id: assetId, kind: latest.kind, latest_version: latest }, version: generated });
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason);
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  function selectAsset(candidate: StudioAsset) {
    setSelection({ asset: candidate });
    setError(null);
    setNotice("");
  }

  return (
    <section className="section-block studio-workspace" aria-labelledby="studio-heading">
      <header className="section-heading">
        <h2 id="studio-heading">Студия контента</h2>
        <div className="actions">
          <button type="button" disabled={busy || !!editor} onClick={() => openEditor({ kind: "ARTICLE" })}>Новая статья</button>
          <button type="button" className="secondary" disabled={busy || !!editor} onClick={() => openEditor({ kind: "ILLUSTRATION" })}>Новая иллюстрация</button>
        </div>
      </header>
      {notice && <p className="notice" role="status">{notice}</p>}
      {versionConflict ? (
        <p className="error" role="alert">Материал изменился после открытия редактора. Ваши правки остались в форме.</p>
      ) : error != null && <FriendlyError error={error} fallback="Не удалось выполнить действие в студии." />}
      {conflictingRevision && versionLookupFailure && (
        versionLookupFailure.authenticationError ? (
          <FriendlyError
            error={versionLookupFailure.authenticationError}
            onRetry={() => void retryVersionLookup()}
            retryLabel={RETRY_VERSION_LOOKUP_LABEL}
          />
        ) : (
          <div className="notice">
            <p role="status">Не удалось загрузить актуальную версию.</p>
            <button type="button" className="secondary" disabled={busy} onClick={() => void retryVersionLookup()}>
              {RETRY_VERSION_LOOKUP_LABEL}
            </button>
          </div>
        )
      )}
      {generation.label && (
        <div className="notice studio-generation">
          <p role="status">{generation.label}</p>
          {generation.job?.error_code && <p>Код: <code>{generation.job.error_code}</code></p>}
          {generation.authenticationError ? (
            <FriendlyError error={generation.authenticationError} onRetry={generation.resume} retryLabel="Проверить состояние" />
          ) : generation.stopped && (
            <p>Автоматическая проверка приостановлена. Можно запросить состояние существующей задачи.</p>
          )}
          {failedGeneration?.status === "UNKNOWN" && <p>Новая генерация создаст отдельную задачу.</p>}
          <div className="actions">
            {generation.stopped && !generation.authenticationError && (
              <button type="button" className="secondary" onClick={generation.resume}>Проверить состояние</button>
            )}
            {recoverableDraft && (
              <button type="button" className="secondary" disabled={busy || !!editor} onClick={() => void reopenGenerationDraft()}>Вернуться к брифу</button>
            )}
            {failedGeneration && (
              <button type="button" className="secondary" disabled={busy || !!editor} onClick={dismissGeneration}>Скрыть задачу</button>
            )}
            {generation.job?.status === "SUCCEEDED" && generation.job.asset_id && generation.job.version_id && (
              <button type="button" className="secondary" disabled={busy || !!editor} onClick={() => void openGeneratedVersion()}>Открыть черновик иллюстрации</button>
            )}
          </div>
        </div>
      )}
      <div className="studio-grid">
        <section className="card studio-library" aria-label="Материалы студии">
          <h3>Материалы</h3>
          {listing.loading && <p role="status">Загружаем материалы…</p>}
          {listing.error != null && <FriendlyError error={listing.error} fallback="Не удалось загрузить материалы студии." onRetry={() => listing.refresh()} />}
          {listing.page && assets && (
            <>
              {assets.length === 0 ? <p>В студии пока нет материалов.</p> : (
                <ul className="studio-choice-list">
                  {assets.map((candidate) => (
                    <li key={candidate.id} className="studio-asset">
                      <button type="button" className="secondary" disabled={busy || !!editor} aria-pressed={asset?.id === candidate.id} onClick={() => selectAsset(candidate)}>
                        {candidate.latest_version.payload.title}
                      </button>
                      <div className="actions">
                        <span>{candidate.kind === "ARTICLE" ? "Статья" : "Иллюстрация"} · v{candidate.latest_version.version_number}</span>
                        <VersionBadge version={candidate.latest_version} />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              <StudioPagination page={listing.page} noun="материалы студии" onChange={listing.setOffset} />
            </>
          )}
        </section>
        <div className="studio-main">
          {editor?.kind === "ARTICLE" && (
            <ArticleEditor
              key={editor.previous?.id ?? "new-article"}
              campaignId={campaignId}
              previous={editor.previous}
              busy={busy}
              versionConflict={versionConflict}
              onCancel={() => openEditor()}
              onSave={(input) => void saveArticle(input)}
            />
          )}
          {editor?.kind === "ILLUSTRATION" && (
            <IllustrationEditor
              key={editor.previous?.id ?? "new-illustration"}
              campaignId={campaignId}
              previous={editor.previous}
              busy={busy}
              draft={editor.draft}
              versionConflict={versionConflict}
              generationInProgress={generation.active}
              onCancel={() => openEditor()}
              onSave={(input, change) => void saveIllustration(input, change)}
            />
          )}
          {!editor && asset && version && (
            <>
              <div className="actions">
                <button className="secondary" type="button" disabled={busy} onClick={() => startRevision(asset.latest_version)}>
                  Новая версия на основе v{asset.latest_version.version_number}
                </button>
              </div>
              <StudioVersionDetails campaignId={campaignId} version={version} busy={busy} onApprove={() => void approve(version)} />
              <StudioHistory
                key={asset.id}
                campaignId={campaignId}
                asset={asset}
                selectedVersionId={version.id}
                refreshCount={historyRefresh}
                disabled={busy}
                onSelect={(next) => setSelection({ asset, version: next })}
              />
            </>
          )}
        </div>
      </div>
    </section>
  );
}
