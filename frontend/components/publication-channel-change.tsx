"use client";
import { useState } from "react";
import { contentApi, request } from "@/lib/api";

type Row = { publication_id: string; content_item_id: string; content_version_id: string; title: string; channel: "VK" | "TELEGRAM"; scheduled_at: string | null; operational_data_complete: boolean };
type Prepared = { content_item_id: string; content_version_id: string; channel: "VK" | "TELEGRAM" };
export function PublicationChannelChange({ rows, onApplied }: { rows: Row[]; onApplied: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState("");
  const [text, setText] = useState("");
  const [date, setDate] = useState("");
  const [prepared, setPrepared] = useState<Prepared | null>(null);
  const [approved, setApproved] = useState(false);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const row = rows.find((item) => item.publication_id === selected);
  if (!rows.length) return null;
  function choose(id: string) {
    setSelected(id); setPrepared(null); setApproved(false); setAck(false); setText(""); setMessage("");
    const when = rows.find((item) => item.publication_id === id)?.scheduled_at;
    const time = when ? new Date(when) : null;
    setDate(time ? new Date(time.getTime() - time.getTimezoneOffset() * 60000).toISOString().slice(0,16) : "");
  }
  async function act(action: () => Promise<void>) {
    setBusy(true); setMessage(""); setAck(false);
    try { await action(); } catch (error) { setApproved(false); setMessage(error instanceof Error ? error.message : "Проверьте состояние перед повторным действием."); }
    finally { setBusy(false); }
  }
  return <section className="section-block" aria-label="Смена канала публикации">
    <button className="secondary" onClick={() => setOpen(!open)}>Изменить канал публикации</button>
    {open && <div className="content-card"><p>Подготовьте текст для другого канала и согласуйте его отдельно. Исходная отправка останется в расписании до подтверждения замены и может выполниться в прежнее время.</p>
      <label>Выберите публикацию<select disabled={busy} value={selected} onChange={(event) => choose(event.target.value)}><option value="">Выберите пост</option>{rows.map((item) => <option disabled={!item.operational_data_complete} key={item.publication_id} value={item.publication_id}>{item.title} · {item.channel}</option>)}</select></label>
      {row && <>
        <p>Новый канал: {row.channel === "VK" ? "Telegram" : "VK"}. <a href={`/content/${row.content_item_id}`} target="_blank" rel="noreferrer">Открыть исходный материал</a></p>
        {!prepared && <><label>Текст для нового канала<textarea disabled={busy} value={text} onChange={(event) => setText(event.target.value)} /></label>
          <button disabled={busy || !text.trim()} onClick={() => void act(async () => {
            setPrepared(await request<Prepared>(`/publications/${row.publication_id}/channel-change/prepare`, { method: "POST", body: JSON.stringify({ expected_content_version_id: row.content_version_id, channel: row.channel === "VK" ? "TELEGRAM" : "VK", content: text }) }));
            setMessage("Отдельный текст создан для согласования. Исходная отправка не изменена.");
          })}>Подготовить текст для согласования</button>
          <button className="secondary" disabled={busy} onClick={() => void act(async () => {
            const found = await request<Prepared | null>(`/publications/${row.publication_id}/channel-change`);
            setPrepared(found); if (!found) setMessage("Подготовленной адаптации ещё нет.");
          })}>Найти ранее подготовленный текст</button>
        </>}
        {prepared && <><p><a href={`/content/${prepared.content_item_id}`} target="_blank" rel="noreferrer">Открыть текст и согласовать</a></p>
          <button disabled={busy} onClick={() => void act(async () => {
            const content = await contentApi.get(prepared.content_item_id);
            const ready = content.status === "APPROVED" && !!content.current_version && content.approved_version_id === content.current_version.id;
            setApproved(ready); if (content.current_version) setPrepared({ ...prepared, content_version_id: content.current_version.id });
            setMessage(ready ? "Точная версия текста согласована. Проверьте дату и подтвердите замену." : "Текст пока не согласован в текущей версии.");
          })}>Проверить согласование текста</button>
          {approved && <><label>Дата новой отправки<input type="datetime-local" disabled={busy} value={date} onChange={(event) => { setDate(event.target.value); setAck(false); }} /></label>
            <label><input type="checkbox" disabled={busy} checked={ack} onChange={(event) => setAck(event.target.checked)} /> Подтверждаю замену: отправка в прежний канал будет отменена, новый текст запланирован в другой канал.</label>
            <button disabled={busy || !ack || !date} onClick={() => void act(async () => {
              const when = new Date(date); if (!Number.isFinite(when.getTime())) throw new Error("Проверьте дату отправки.");
              await request(`/publications/${row.publication_id}/channel-change/confirm`, { method: "POST", body: JSON.stringify({ content_item_id: prepared.content_item_id, expected_content_version_id: prepared.content_version_id, scheduled_at: when.toISOString(), confirmed: true }) });
              setApproved(false); setPrepared(null); setSelected(""); setMessage("Канал заменён. Прежняя отправка отменена, новая запланирована."); await onApplied();
            })}>Подтвердить замену канала</button>
          </>}
        </>}
      </>}
      {message && <p role="status">{message}</p>}
    </div>}
  </section>;
}
