"use client";

import { type FormEvent, useState } from "react";

function toLocalDateTimeInput(value: Date): string {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function initialDateTime(value?: string | null): string {
  if (value) return toLocalDateTimeInput(new Date(value));
  const future = new Date(Date.now() + 60 * 60 * 1000);
  future.setMinutes(0, 0, 0);
  return toLocalDateTimeInput(future);
}

interface PublicationScheduleEditorProps {
  editorKey: string;
  title: string;
  submitLabel: string;
  initialScheduledAt?: string | null;
  busy: boolean;
  onClose: () => void;
  onSubmit: (scheduledAt: string) => Promise<boolean>;
}

export function PublicationScheduleEditor({
  editorKey,
  title,
  submitLabel,
  initialScheduledAt,
  busy,
  onClose,
  onSubmit,
}: PublicationScheduleEditorProps) {
  const [value, setValue] = useState(() => initialDateTime(initialScheduledAt));
  const [minimum] = useState(() => toLocalDateTimeInput(new Date(Date.now() + 60_000)));
  const [error, setError] = useState("");
  const inputId = `schedule-${editorKey.replaceAll(":", "-")}`;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime()) || parsed.getTime() <= Date.now()) {
      setError("Выберите корректные будущие дату и время.");
      return;
    }
    setError("");
    if (await onSubmit(parsed.toISOString())) onClose();
  }

  return <form className="panel" aria-label={title} onSubmit={submit}>
    <h4>{title}</h4>
    <label htmlFor={inputId}>Дата и время публикации</label>
    <input
      id={inputId}
      type="datetime-local"
      min={minimum}
      step="300"
      value={value}
      onChange={(event) => setValue(event.target.value)}
      required
      autoFocus
    />
    <p className="subtle">Используется локальный часовой пояс браузера. Выберите дату в календаре и время с шагом пять минут.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="actions">
      <button type="submit" disabled={busy}>{submitLabel}</button>
      <button type="button" className="secondary" disabled={busy} onClick={onClose}>Закрыть</button>
    </div>
  </form>;
}
