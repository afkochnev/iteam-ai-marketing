"use client";

import { FormEvent, useState } from "react";

import type { CampaignInput } from "@/lib/api";

const EMPTY: CampaignInput = { name: "", goal: "", product: "", target_audience: "", offer: "", desired_result: "", description: "", start_date: "", end_date: "" };

export function CampaignForm({ initial = EMPTY, submitLabel, onSubmit, onChange }: { initial?: CampaignInput; submitLabel: string; onSubmit: (values: CampaignInput) => Promise<void>; onChange?: () => void }) {
  const [values, setValues] = useState<CampaignInput>({ ...EMPTY, ...initial });
  const [error, setError] = useState(""); const [submitting, setSubmitting] = useState(false);
  function set(field: keyof CampaignInput, value: string) { onChange?.(); setValues((current) => ({ ...current, [field]: value })); }
  async function submit(event: FormEvent) {
    event.preventDefault(); setError("");
    if (!values.name.trim()) { setError("Название обязательно."); return; }
    if (!values.goal.trim()) { setError("Цель обязательна."); return; }
    if (values.start_date && values.end_date && values.start_date > values.end_date) { setError("Дата окончания не может быть раньше даты начала."); return; }
    setSubmitting(true);
    try { await onSubmit(values); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить кампанию."); } finally { setSubmitting(false); }
  }
  return <form className="campaign-form" onSubmit={submit} noValidate>
    <fieldset className="form-section"><legend>Основные параметры</legend>
      <p className="muted">Название и описание можно изменить без пересмотра маркетинговой логики.</p>
      <label htmlFor="name">Название *</label><input id="name" value={values.name} onChange={(e) => set("name", e.target.value)} />
      <label htmlFor="description">Описание</label><textarea id="description" value={values.description ?? ""} onChange={(e) => set("description", e.target.value)} />
      <div className="date-grid"><div><label htmlFor="start">Дата начала</label><input id="start" type="date" value={values.start_date ?? ""} onChange={(e) => set("start_date", e.target.value)} /></div><div><label htmlFor="end">Дата окончания</label><input id="end" type="date" value={values.end_date ?? ""} onChange={(e) => set("end_date", e.target.value)} /></div></div>
    </fieldset>
    <fieldset className="form-section"><legend>Маркетинговые вводные</legend>
      <p className="muted">Изменения здесь могут потребовать новой версии стратегии и проверки медиаплана.</p>
      <label htmlFor="goal">Цель *</label><textarea id="goal" value={values.goal} onChange={(e) => set("goal", e.target.value)} />
      <label htmlFor="product">Продукт</label><input id="product" value={values.product ?? ""} onChange={(e) => set("product", e.target.value)} />
      <label htmlFor="audience">Целевая аудитория</label><textarea id="audience" value={values.target_audience ?? ""} onChange={(e) => set("target_audience", e.target.value)} />
      <label htmlFor="offer">Предложение / оффер</label><textarea id="offer" value={values.offer ?? ""} onChange={(e) => set("offer", e.target.value)} />
      <label htmlFor="result">Желаемый результат</label><input id="result" value={values.desired_result ?? ""} onChange={(e) => set("desired_result", e.target.value)} />
    </fieldset>
    {error && <p role="alert" className="error">{error}</p>}<button type="submit" disabled={submitting}>{submitting ? "Сохраняем…" : submitLabel}</button>
  </form>;
}
