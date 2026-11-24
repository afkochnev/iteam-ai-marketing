"use client";

import { useState, type FormEvent } from "react";
import { StudioVersionPicker } from "@/components/studio/version-picker";
import type { StudioArticleInput, StudioIllustrationInput, StudioVersion } from "@/lib/studio";

type VersionOf<K> = Extract<StudioVersion, { kind: K }>;

function textValue(fields: FormData, name: string) {
  return String(fields.get(name) ?? "").trim();
}

function initialReference(id?: string | null) {
  return id ? { id } : undefined;
}

export function ArticleEditor({
  campaignId,
  previous,
  busy,
  versionConflict,
  onSave,
  onCancel,
}: {
  campaignId: string;
  previous?: VersionOf<"ARTICLE">;
  busy: boolean;
  versionConflict: boolean;
  onSave: (input: StudioArticleInput) => void;
  onCancel: () => void;
}) {
  const [source, setSource] = useState(initialReference(previous?.source_version_id));
  const [illustration, setIllustration] = useState(initialReference(previous?.illustration_version_id));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || versionConflict) return;
    const fields = new FormData(event.currentTarget);
    onSave({
      title: textValue(fields, "title"),
      body: textValue(fields, "body"),
      source_version_id: source?.id,
      illustration_version_id: illustration?.id,
    });
  }

  return (
    <form className="card studio-editor" onSubmit={submit}>
      <fieldset disabled={busy}>
        <legend>{previous ? `Новая редакция статьи на основе v${previous.version_number}` : "Новая статья"}</legend>
        <label>Название статьи<input name="title" maxLength={255} defaultValue={previous?.payload.title} required /></label>
        <label>Текст статьи<textarea name="body" maxLength={100_000} rows={10} defaultValue={previous?.payload.body} required /></label>
        <StudioVersionPicker campaignId={campaignId} label="Исходная версия" value={source} onChange={setSource} />
        <StudioVersionPicker
          campaignId={campaignId}
          label="Версия иллюстрации"
          value={illustration}
          onChange={setIllustration}
          approvedIllustrations
        />
        <div className="actions">
          <button type="submit" disabled={versionConflict}>{busy ? "Сохраняем…" : "Сохранить черновик статьи"}</button>
          <button type="button" className="secondary" onClick={onCancel}>Отмена</button>
        </div>
      </fieldset>
    </form>
  );
}

export type IllustrationChange = { method: "upload"; file: File } | { method: "keep" | "generate" };

export function IllustrationEditor({
  campaignId,
  previous,
  busy,
  draft,
  versionConflict,
  generationInProgress,
  onSave,
  onCancel,
}: {
  campaignId: string;
  previous?: VersionOf<"ILLUSTRATION">;
  busy: boolean;
  draft?: StudioIllustrationInput;
  versionConflict: boolean;
  generationInProgress: boolean;
  onSave: (input: StudioIllustrationInput, change: IllustrationChange) => void;
  onCancel: () => void;
}) {
  const [method, setMethod] = useState<IllustrationChange["method"]>(draft ? "generate" : previous ? "keep" : "upload");
  const [source, setSource] = useState(initialReference(draft ? draft.source_version_id : previous?.source_version_id));
  const payload = draft ?? previous?.payload;
  const [fileError, setFileError] = useState("");
  const generationBlocked = method === "generate" && generationInProgress;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (generationBlocked || versionConflict) return;
    const fields = new FormData(event.currentTarget);
    const input = {
      title: textValue(fields, "title"),
      brief: textValue(fields, "brief"),
      brand_profile: textValue(fields, "brand_profile"),
      alt_text: textValue(fields, "alt_text"),
      usage_rights: textValue(fields, "usage_rights"),
      caption: textValue(fields, "caption") || undefined,
      source_version_id: source?.id,
    };
    if (method !== "upload") {
      onSave(input, { method });
      return;
    }
    const fileInput = event.currentTarget.elements.namedItem("file");
    const file = fileInput instanceof HTMLInputElement ? fileInput.files?.[0] : undefined;
    if (!file || file.size === 0) {
      setFileError("Выберите изображение для загрузки.");
      return;
    }
    setFileError("");
    onSave(input, { method, file });
  }

  return (
    <form className="card studio-editor" onSubmit={submit}>
      <fieldset disabled={busy}>
        <legend>{previous ? `Новая версия иллюстрации на основе v${previous.version_number}` : "Новая иллюстрация"}</legend>
        <label>Название иллюстрации<input name="title" maxLength={255} defaultValue={payload?.title} required /></label>
        <label>Бриф иллюстрации<textarea name="brief" maxLength={5_000} rows={4} defaultValue={payload?.brief} required /></label>
        <label>Профиль бренда<textarea name="brand_profile" maxLength={2_000} rows={3} defaultValue={payload?.brand_profile} required /></label>
        <label>Альтернативный текст<input name="alt_text" maxLength={300} defaultValue={payload?.alt_text} required /></label>
        <label>Подпись<input name="caption" maxLength={1_000} defaultValue={payload?.caption ?? ""} /></label>
        <label>Права использования<textarea name="usage_rights" maxLength={1_000} rows={2} defaultValue={payload?.usage_rights} required /></label>
        <StudioVersionPicker campaignId={campaignId} label="Исходная версия" value={source} onChange={setSource} />
        <label>Как получить изображение
          <select value={method} onChange={(event) => {
            const value = event.target.value;
            if (value === "upload" || value === "generate" || (value === "keep" && previous)) setMethod(value);
          }}>
            {previous && <option value="keep">Сохранить изображение из предыдущей версии</option>}
            <option value="upload">Загрузить своё</option>
            <option value="generate">Сгенерировать по брифу</option>
          </select>
        </label>
        {method === "upload" && <label>Файл изображения<input name="file" type="file" accept="image/png,image/jpeg,image/webp" required /></label>}
        {generationBlocked && <p className="notice">В этой студии уже выполняется генерация. Её состояние показано выше.</p>}
        {fileError && method === "upload" && <p className="error" role="alert">{fileError}</p>}
        <div className="actions">
          <button type="submit" disabled={generationBlocked || versionConflict}>
            {!busy && method === "keep" && "Сохранить новую версию"}
            {!busy && method === "upload" && "Загрузить черновик иллюстрации"}
            {!busy && method === "generate" && "Начать генерацию"}
            {busy && "Отправляем…"}
          </button>
          <button type="button" className="secondary" onClick={onCancel}>Отмена</button>
        </div>
      </fieldset>
    </form>
  );
}
