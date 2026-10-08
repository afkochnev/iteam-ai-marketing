"use client";

import { useEffect, useRef, useState } from "react";
import { campaignKpisApi, type CampaignKPI, type CampaignPerformance, type KPIConfiguration, type KPIMetric } from "@/lib/api";

const metrics: KPIMetric[] = ["VIEWS", "IMPRESSIONS", "REACTIONS", "LIKES", "COMMENTS", "SHARES", "CLICKS", "SUBSCRIBERS", "ENGAGEMENT_RATE", "CTR"];
const valueText = (value: number | string | null | undefined) => value == null ? "Нет данных" : String(value);
const localTime = (value: string) => { const date = new Date(value); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };

export function CampaignKPIBlock({ campaignId, archived, performance, onChange }: { campaignId: string; archived: boolean; performance: CampaignPerformance | null; onChange: () => void }) {
  const [rows, setRows] = useState<CampaignKPI[]>([]);
  const [editing, setEditing] = useState<CampaignKPI | null>(null);
  const [open, setOpen] = useState(false);
  const [metric, setMetric] = useState<KPIMetric>("VIEWS");
  const [channel, setChannel] = useState("");
  const [target, setTarget] = useState("0");
  const [comparison, setComparison] = useState<"GTE" | "LTE">("GTE");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const reload = () => campaignKpisApi.list(campaignId).then(setRows);
  useEffect(() => { let active = true; void campaignKpisApi.list(campaignId).then((data) => { if (active) setRows(data); }).catch((reason: Error) => { if (active) setError(reason.message); }); return () => { active = false; }; }, [campaignId]);
  function form(row: CampaignKPI | null) {
    setEditing(row); setMetric(row?.metric ?? "VIEWS"); setChannel(row?.channel ?? ""); setTarget(String(row?.target_value ?? 0)); setComparison(row?.comparison ?? "GTE"); setStart(row ? localTime(row.period_start) : ""); setEnd(row ? localTime(row.period_end) : ""); setDescription(row?.description ?? ""); setError(""); setOpen(true);
  }
  const valid = start && end && new Date(start).getTime() < new Date(end).getTime() && target.trim() !== "" && Number.isFinite(Number(target)) && Number(target) >= 0;
  async function save() {
    if (pending.current || archived || !valid) return;
    pending.current = true; setBusy(true); setError("");
    const data: KPIConfiguration = { metric, channel: channel ? channel as "TELEGRAM" | "VK" : null, target_value: target, comparison, period_start: new Date(start).toISOString(), period_end: new Date(end).toISOString(), description: description || null };
    try { if (editing) await campaignKpisApi.update(editing.id, data); else await campaignKpisApi.create(campaignId, data); setOpen(false); await reload(); onChange(); } catch (reason) { setError((reason as Error).message); } finally { pending.current = false; setBusy(false); }
  }
  async function remove(row: CampaignKPI) {
    if (pending.current || archived) return;
    pending.current = true; setBusy(true); setError("");
    try { await campaignKpisApi.delete(row.id); await reload(); onChange(); } catch (reason) { setError((reason as Error).message); } finally { pending.current = false; setBusy(false); }
  }
  const active = rows.filter((row) => row.is_active);
  return <section aria-label="Цели кампании"><h3>Цели кампании</h3>
    <p>CTR = clicks / impressions. ENGAGEMENT_RATE = (reactions + comments + shares) / impressions. Значения — доли, например 0.1 = 10%. Для расчёта нужны все исходные поля и impressions &gt; 0; неполные наблюдения исключаются.</p>
    {error && <p role="alert">{error}</p>}
    {!active.length && <p>KPI пока не настроены.</p>}
    <button className="secondary" disabled={archived || busy || active.length >= 5} onClick={() => form(null)}>Добавить KPI</button>
    {active.length >= 5 && <p>Максимум пять активных KPI.</p>}
    {active.map((row) => { const fact = performance?.kpis?.find((item) => item.kpi_id === row.id); return <article className="card" key={row.id}>
      <h4>{row.metric} · {row.channel ?? "Все каналы"}</h4>
      <p>Цель: {row.comparison === "GTE" ? "≥" : "≤"} {valueText(row.target_value)} · Факт: {valueText(fact?.observed_value)}</p>
      <p>Период: {new Date(row.period_start).toLocaleString()} — {new Date(row.period_end).toLocaleString()}</p>
      <p>Покрытие: {fact ? `${fact.observed_publication_count}/${fact.eligible_publication_count} (${Math.round(fact.coverage_ratio * 100)}%)` : "Нет данных"}</p>
      <p>{fact?.target_met == null ? "недостаточно данных" : fact.target_met ? "достигнута" : "не достигнута"}</p>
      {fact?.limitation && <p>{fact.limitation}</p>}{row.description && <p>{row.description}</p>}
      <button className="secondary" disabled={archived || busy} onClick={() => form(row)}>Редактировать KPI</button>
      <button className="secondary" disabled={archived || busy} onClick={() => void remove(row)}>Удалить KPI</button>
    </article>; })}
    {open && <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <label>Метрика<select value={metric} disabled={archived || busy} onChange={(event) => setMetric(event.target.value as KPIMetric)}>{metrics.map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>Канал<select value={channel} disabled={archived || busy} onChange={(event) => setChannel(event.target.value)}><option value="">Все каналы</option><option>TELEGRAM</option><option>VK</option></select></label>
      <label>Целевое значение<input type="number" min="0" step="any" required value={target} disabled={archived || busy} onChange={(event) => setTarget(event.target.value)} /></label>
      <label>Сравнение<select value={comparison} disabled={archived || busy} onChange={(event) => setComparison(event.target.value as "GTE" | "LTE")}><option value="GTE">≥</option><option value="LTE">≤</option></select></label>
      <label>Начало периода (местное время)<input type="datetime-local" required value={start} disabled={archived || busy} onChange={(event) => setStart(event.target.value)} /></label>
      <label>Конец периода (местное время)<input type="datetime-local" required value={end} disabled={archived || busy} onChange={(event) => setEnd(event.target.value)} /></label>
      <label>Описание<input value={description} maxLength={2000} disabled={archived || busy} onChange={(event) => setDescription(event.target.value)} /></label>
      <button disabled={!valid || archived || busy}>Сохранить KPI</button><button type="button" className="secondary" onClick={() => setOpen(false)}>Закрыть</button>
    </form>}
  </section>;
}

export function PublicationPerformanceTable({ performance }: { performance: CampaignPerformance }) {
  return <div style={{ overflowX: "auto" }}><table aria-label="Эффективность публикаций"><thead><tr>{["Публикация", "Канал", "Дата публикации", "ContentVersion", "Просмотры", "Показы", "Реакции", "Комментарии", "Репосты", "Клики", "Источник", "Время наблюдения"].map((title) => <th key={title}>{title}</th>)}</tr></thead><tbody>{performance.publications.map((row) => <tr key={row.publication_id}>
    <td><a href={`/content/${row.content_item_id}`}>{row.content_title ?? "Материал"}</a></td><td>{row.channel}</td><td>{row.published_at ? new Date(row.published_at).toLocaleString() : "Нет данных"}</td><td title={row.content_version_id}>v{row.version_number ?? "—"} · {row.content_version_id.slice(0, 8)}</td>
    {(["views", "impressions", "reactions", "comments", "shares", "clicks"] as const).map((field) => <td key={field}>{valueText(row[field] ?? row.metrics?.[field])}</td>)}
    <td>{row.source ?? row.metrics?.source ?? "Нет данных"}</td><td>{row.observed_at ?? row.metrics?.observed_at ?? "Нет данных"}</td>
  </tr>)}</tbody></table></div>;
}
