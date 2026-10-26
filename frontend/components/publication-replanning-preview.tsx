"use client";

import { useState } from "react";
import { request } from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";

type Channel = "VK" | "TELEGRAM";
type Row = {
  publication_id: string; title: string; channel: Channel; scheduled_at: string | null;
  campaign: { id: string; name: string }; operational_data_complete: boolean;
};
type Change = { publication_id: string; scheduled_at: string; channel: Channel };
type Preview = { mode: "PREVIEW_ONLY"; message: string; items: Array<{
  publication_id: string; original_scheduled_at: string | null; original_channel: Channel;
  proposed_scheduled_at: string; proposed_channel: Channel; blockers: string[]; warnings: string[];
}> };

function localTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export function PublicationReplanningPreview({ rows }: { rows: Row[] }) {
  const [open, setOpen] = useState(false);
  const [changes, setChanges] = useState<Record<string, { time: string; channel: Channel }>>({});
  const [result, setResult] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const campaignIds = new Set(rows.filter((row) => changes[row.publication_id]).map((row) => row.campaign.id));
  if (!rows.length) return null;
  function edit(id: string, value: { time: string; channel: Channel } | null) {
    setChanges((previous) => { const next = { ...previous }; if (value) next[id] = value; else delete next[id]; return next; });
    setResult(null); setError("");
  }
  async function preview() {
    setError(""); setResult(null);
    if (campaignIds.size !== 1) { setError("Выберите публикации одной кампании."); return; }
    const payload: Change[] = [];
    for (const row of rows.filter((row) => changes[row.publication_id])) {
      const value = changes[row.publication_id];
      const time = new Date(value.time);
      if (!value.time || !Number.isFinite(time.getTime())) { setError("Укажите дату и время для каждого выбранного поста."); return; }
      payload.push({ publication_id: row.publication_id, scheduled_at: time.toISOString(), channel: value.channel });
    }
    setBusy(true);
    try {
      setResult(await request<Preview>(`/publications/campaign/${[...campaignIds][0]}/replanning-preview`, { method: "POST", body: JSON.stringify({ changes: payload }) }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Предпросмотр недоступен."); }
    finally { setBusy(false); }
  }
  return <section className="section-block" aria-label="Предпросмотр перепланирования">
    <button className="secondary" onClick={() => setOpen(!open)}>Предпросмотр перепланирования</button>
    {open && <div className="content-card">
      <p>Выберите посты одной кампании и предложите новые даты или каналы. Время — в часовом поясе браузера. Этот шаг ничего не меняет в расписании.</p>
      {rows.map((row) => { const value = changes[row.publication_id]; return <fieldset key={row.publication_id} disabled={busy || !row.operational_data_complete}>
        <legend>{row.title} · {row.campaign.name}</legend>
        <label><input type="checkbox" checked={!!value} onChange={(event) => edit(row.publication_id, event.target.checked ? { time: localTime(row.scheduled_at), channel: row.channel } : null)} /> Включить в предпросмотр</label>
        {value && <div className="actions">
          <label>Новая дата и время<input type="datetime-local" value={value.time} onChange={(event) => edit(row.publication_id, { ...value, time: event.target.value })} /></label>
          <label>Новый канал<select value={value.channel} onChange={(event) => edit(row.publication_id, { ...value, channel: event.target.value as Channel })}><option value="VK">VK</option><option value="TELEGRAM">Telegram</option></select></label>
        </div>}
      </fieldset>; })}
      <button disabled={busy || !Object.keys(changes).length} onClick={() => void preview()}>{busy ? "Проверяем…" : "Проверить изменения без сохранения"}</button>
      {error && <p role="alert">{error}</p>}
      {result && <div role="status"><p>{result.message}</p>{result.items.map((item) => <div key={item.publication_id}>
        <strong>{rows.find((row) => row.publication_id === item.publication_id)?.title}</strong>
        <p>Было: {item.original_channel} · {item.original_scheduled_at ? formatDateTime(item.original_scheduled_at) : "Без даты"}</p>
        <p>Предложено: {item.proposed_channel} · {formatDateTime(item.proposed_scheduled_at)}</p>
        {item.blockers.map((text) => <p key={text}>Требуется решение: {text}</p>)}
        {item.warnings.map((text) => <p key={text}>{text}</p>)}
      </div>)}</div>}
    </div>}
  </section>;
}
