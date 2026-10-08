"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { experimentsApi, optimizationApi, publicationsApi, type ExperimentConfiguration, type MarketingExperiment, type OptimizationAction, type Publication } from "@/lib/api";
import { FriendlyError } from "@/components/friendly-error";

const refreshEvent = "marketing-experiment-created";
const states = {DRAFT: "Черновик", APPROVED: "Утверждён", RUNNING: "Наблюдение идёт", COMPLETED: "Завершён", CANCELLED: "Отменён"};
const dateLabels = {baseline_start: "Начало baseline (местное время)", baseline_end: "Конец baseline (местное время)", experiment_start: "Начало experiment (местное время)", experiment_end: "Конец experiment (местное время)"};

export function ExperimentCreateForm({action, campaignId, archived, onCreated}: {action: OptimizationAction; campaignId: string; archived: boolean; onCreated: () => Promise<void> | void}) {
  const [publications, setPublications] = useState<Publication[]>([]);
  const [dates, setDates] = useState({baseline_start: "", baseline_end: "", experiment_start: "", experiment_end: ""});
  const [baseline, setBaseline] = useState<string[]>([]);
  const [experiment, setExperiment] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const spec = action.experiment_spec;
  useEffect(() => {let active = true; void publicationsApi.listCampaign(campaignId).then(rows => {if (active) setPublications(rows);}).catch(reason => {if (active) setError(reason);}); return () => {active = false;};}, [campaignId]);
  if (!spec) return null;
  const times = Object.values(dates).map(value => value ? new Date(value).getTime() : NaN);
  const completeDates = times.every(Number.isFinite);
  const validDates = completeDates && times[0] < times[1] && times[1] <= times[2] && times[2] < times[3];
  function choose(id: string, checked: boolean, group: "BASELINE" | "EXPERIMENT") {
    const update = group === "BASELINE" ? setBaseline : setExperiment;
    update(current => checked ? [...current, id] : current.filter(value => value !== id));
  }
  async function create() {
    if (pending.current || archived || !validDates || !baseline.length || !experiment.length) return;
    pending.current = true; setBusy(true); setError(null);
    try {
      const config: ExperimentConfiguration = {...Object.fromEntries(Object.entries(dates).map(([key, value]) => [key, new Date(value).toISOString()])) as Pick<ExperimentConfiguration, "baseline_start" | "baseline_end" | "experiment_start" | "experiment_end">, baseline_publication_ids: baseline, experiment_publication_ids: experiment};
      await optimizationApi.apply(action.id, null, config);
      window.dispatchEvent(new Event(refreshEvent));
      await onCreated();
    } catch (reason) {setError(reason);}
    finally {pending.current = false; setBusy(false);}
  }
  return <section aria-label="Параметры эксперимента">
    <p className="notice">Эксперимент фиксирует наблюдение и не изменяет публикации автоматически.</p>
    <label>Гипотеза<textarea readOnly value={spec.hypothesis} /></label>
    <label>Предлагаемое изменение<textarea readOnly value={spec.proposed_change} /></label>
    <label>Метрика<input readOnly value={spec.success_metric} /></label>
    {spec.minimum_observation_requirement && <p>Минимальное требование: {spec.minimum_observation_requirement}</p>}
    {Object.entries(dateLabels).map(([key, label]) => <label key={key}>{label}<input type="datetime-local" required disabled={busy || archived} value={dates[key as keyof typeof dates]} onChange={event => setDates(current => ({...current, [key]: event.target.value}))} /></label>)}
    {completeDates && !validDates && <p role="alert">Периоды должны быть последовательными: начало baseline &lt; конец baseline ≤ начало experiment &lt; конец experiment.</p>}
    {(["BASELINE", "EXPERIMENT"] as const).map(role => <fieldset key={role} disabled={busy || archived}><legend>{role === "BASELINE" ? "Baseline публикации" : "Experiment публикации"}</legend>{publications.map(publication => <label key={publication.id}><input type="checkbox" checked={(role === "BASELINE" ? baseline : experiment).includes(publication.id)} disabled={(role === "BASELINE" ? experiment : baseline).includes(publication.id)} onChange={event => choose(publication.id, event.target.checked, role)} />{publication.id} · {publication.channel} · {publication.status} · версия {publication.content_version_id}</label>)}</fieldset>)}
    {!publications.length && <p>Для эксперимента нужны существующие публикации обеих групп.</p>}
    {Boolean(error) && <FriendlyError error={error} />}
    <button disabled={busy || archived || !validDates || !baseline.length || !experiment.length} onClick={create}>Создать черновик эксперимента</button>
  </section>;
}

export function MarketingExperimentsPanel({campaignId, archived}: {campaignId: string; archived: boolean}) {
  const [rows, setRows] = useState<MarketingExperiment[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const reload = useCallback(async () => {setRows(await experimentsApi.list(campaignId));}, [campaignId]);
  useEffect(() => {
    let active = true;
    const load = () => {void experimentsApi.list(campaignId).then(value => {if (active) setRows(value);}).catch(reason => {if (active) setError(reason);});};
    load(); window.addEventListener(refreshEvent, load);
    return () => {active = false; window.removeEventListener(refreshEvent, load);};
  }, [campaignId]);
  async function decide(id: string, decision: "approve" | "cancel") {
    if (pending.current || archived) return;
    pending.current = true; setBusy(true); setError(null);
    try {await experimentsApi[decision](id); await reload();}
    catch (reason) {setError(reason);}
    finally {pending.current = false; setBusy(false);}
  }
  return <section aria-label="Маркетинговые эксперименты"><h3>Маркетинговые эксперименты</h3>
    {Boolean(error) && <FriendlyError error={error} />}
    {!rows.length && <p>Эксперименты ещё не созданы.</p>}
    {rows.map(row => <article className="card" id={`experiment-${row.id}`} tabIndex={-1} key={row.id} aria-label={`Эксперимент: ${row.hypothesis}`}>
      <h4>{row.hypothesis}</h4><p>{row.proposed_change}</p><p>Метрика: {row.success_metric} · Статус: {states[row.status]}</p>
      <p>Baseline: {new Date(row.baseline_start).toLocaleString()} — {new Date(row.baseline_end).toLocaleString()}</p>
      <p>Experiment: {new Date(row.experiment_start).toLocaleString()} — {new Date(row.experiment_end).toLocaleString()}</p>
      {row.minimum_observation_requirement && <p>Минимальное требование: {row.minimum_observation_requirement}</p>}
      {(["BASELINE", "EXPERIMENT"] as const).map(role => <div key={role}><strong>{role === "BASELINE" ? "Baseline публикации" : "Experiment публикации"}</strong><ul>{row.publications.filter(link => link.role === role).map(link => <li key={link.publication_id}>Публикация {link.publication_id} · точная версия {link.content_version_id}</li>)}</ul></div>)}
      <p>Источник: <Link href={`/campaigns/${campaignId}/performance#action-${row.source_optimization_action_id}`}>Рекомендация {row.source_optimization_action_id.slice(0, 8)}</Link> · <Link href={`/campaigns/${campaignId}/performance#analysis-${row.feedback_analysis_id}`}>Анализ {row.feedback_analysis_id.slice(0, 8)}</Link></p>
      {row.result_summary && <p>{row.result_summary}</p>}
      {row.limitations.length > 0 && <ul aria-label="Ограничения">{row.limitations.map(value => <li key={value}>{value}</li>)}</ul>}
      <p>Сравнение описательное; причинный эффект и статистическая значимость не оцениваются.</p>
      {row.status === "DRAFT" && <button disabled={busy || archived} onClick={() => decide(row.id, "approve")}>Утвердить эксперимент</button>}
      {["DRAFT", "APPROVED", "RUNNING"].includes(row.status) && <button className="secondary" disabled={busy || archived} onClick={() => decide(row.id, "cancel")}>Отменить</button>}
    </article>)}
  </section>;
}
