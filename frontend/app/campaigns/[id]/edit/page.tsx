"use client";

import Link from "@/components/hash-link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { CampaignForm } from "@/components/campaign-form";
import {
  campaignsApi,
  type Campaign,
  type CampaignChangePreview,
  type CampaignInput,
  type CampaignWorkspace,
} from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";
import { PLAN_STATUS_LABELS } from "@/lib/presentation";

export default function EditCampaignPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [workspace, setWorkspace] = useState<CampaignWorkspace | null>(null);
  const [preview, setPreview] = useState<CampaignChangePreview | null>(null);
  const [pendingValues, setPendingValues] = useState<CampaignInput | null>(null);
  const [comment, setComment] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/login");
      return;
    }
    if (!user) return;
    void Promise.all([campaignsApi.get(id), campaignsApi.workspace(id)])
      .then(([campaignValue, workspaceValue]) => {
        if (campaignValue.status === "ARCHIVED") {
          router.replace(`/campaigns/${id}`);
          return;
        }
        setCampaign(campaignValue);
        setWorkspace(workspaceValue);
      })
      .catch((reason: Error) => setError(reason.message));
  }, [authLoading, user, id, router]);

  if (authLoading || (!campaign && !error)) return <div><p>Загружаем кампанию…</p></div>;
  if (!campaign) return <div><section><p role="alert" className="error">{error}</p><Link href="/campaigns">К кампаниям</Link></section></div>;

  const initial: CampaignInput = {
    name: campaign.name,
    description: campaign.description,
    goal: campaign.goal,
    product: campaign.product,
    target_audience: campaign.target_audience,
    offer: campaign.offer,
    desired_result: campaign.desired_result,
    start_date: campaign.start_date,
    end_date: campaign.end_date,
  };

  async function previewChanges(values: CampaignInput) {
    setError("");
    const result = await campaignsApi.previewChange(id, values);
    if (!result.changes.length) {
      setPreview(null);
      setPendingValues(null);
      throw new Error("Нет изменений для сохранения.");
    }
    setPendingValues(values);
    setPreview(result);
  }

  async function save() {
    if (!pendingValues || !preview || !campaign) return;
    setSaving(true);
    setError("");
    try {
      await campaignsApi.applyChange(id, {
        changes: pendingValues,
        comment: comment || null,
        confirmed_impact: preview.requires_confirmation,
        expected_updated_at: campaign.updated_at,
      });
      router.push(`/campaigns/${id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось сохранить изменения.");
    } finally {
      setSaving(false);
    }
  }

  const activePlan = workspace?.publication_plan;
  return <div className="wide">
    <header className="page-header"><div><p className="eyebrow">Управляемая редакция</p><h1>Внести изменения</h1><p className="muted">Сначала проверьте последствия. Утверждённые решения и материалы не переписываются автоматически.</p></div><Link href={`/campaigns/${id}`}>Отмена</Link></header>
    {error && <p role="alert" className="error">{error}</p>}

    <CampaignForm initial={initial} submitLabel="Проверить изменения" onSubmit={previewChanges} onChange={() => { setPreview(null); setPendingValues(null); }} />

    <div className="change-context-grid">
      <section className="page-section" aria-labelledby="strategy-context-heading">
        <p className="eyebrow">Стратегия</p><h2 id="strategy-context-heading">Текущая утверждённая версия</h2>
        <p><strong>Стратегия v{campaign.strategy_version}</strong> · {campaign.strategy ? "действует" : "ещё не сформирована"}</p>
        <p className="muted">Стратегические изменения создадут управленческую редакцию. Эта версия останется source of truth до отдельного утверждения новой стратегии.</p>
        <Link href={`/campaigns/${id}#strategy`}>Открыть стратегию и историю согласований</Link>
      </section>
      <section className="page-section" aria-labelledby="materials-context-heading">
        <p className="eyebrow">Медиаплан и материалы</p><h2 id="materials-context-heading">Связанные решения</h2>
        <ul className="simple-list">
          <li><a href={`/campaigns/${id}#publication-plan`}>Текущий медиаплан</a><span>{activePlan ? `${PLAN_STATUS_LABELS[activePlan.status]}, ${activePlan.item_count} пунктов` : "не создан"}</span></li>
          <li><Link href={`/content?campaign_id=${id}&content_type=ARTICLE`}>Articles</Link><span>{workspace?.articles.length ?? 0}</span></li>
          <li><Link href={`/content?campaign_id=${id}&content_type=SOCIAL_POST`}>Social Posts</Link><span>{workspace?.social_posts.length ?? 0}</span></li>
          <li><Link href="/publications">Будущие Publications</Link><span>{workspace?.director.scheduled_publication_count ?? 0}</span></li>
        </ul>
        <p className="muted">Существующие версии, опубликованная история и расписание сохраняются.</p>
      </section>
    </div>

    {preview && <section className="page-section impact-preview" aria-labelledby="impact-heading">
      <p className="eyebrow">Проверка перед сохранением</p><h2 id="impact-heading">Вы меняете</h2>
      <dl className="change-list">{preview.changes.map((item) => <div key={item.field}><dt>{item.label}</dt><dd><span>{item.old_value || "Не указано"}</span><strong aria-hidden="true">→</strong><span>{item.new_value || "Не указано"}</span></dd></div>)}</dl>
      {preview.warning && <p className="warning"><strong>{preview.warning}</strong></p>}
      <h3>Это может затронуть</h3>
      <div className="summary-grid metric-cards">
        <div className="metric-card"><strong>v{preview.impact.strategy_version}</strong><span>Стратегия</span><small>{preview.impact.strategy_status}</small></div>
        <div className="metric-card"><strong>{preview.impact.future_plan_item_count}</strong><span>Будущих пунктов медиаплана</span><small>{preview.impact.publication_plan_status ? `${PLAN_STATUS_LABELS[preview.impact.publication_plan_status]} · ${preview.impact.publication_plan_item_count} активных` : "Плана нет"}</small></div>
        <div className="metric-card"><strong>{preview.impact.approved_social_post_count}</strong><span>Утверждённых Social Posts</span></div>
        <div className="metric-card"><strong>{preview.impact.scheduled_publication_count}</strong><span>Запланированных Publications</span></div>
      </div>
      {preview.impact.scheduled_publications.length > 0 && <>
        <h3>Затронутые запланированные публикации</h3>
        <ul className="simple-list">{preview.impact.scheduled_publications.map((publication) => <li key={publication.id}><span>{publication.channel}</span><span>{formatDateTime(publication.scheduled_at)}</span></li>)}</ul>
        <p className="muted">Они останутся в текущем расписании. Перенос или отмена требуют отдельного решения.</p>
      </>}
      <h3>После сохранения</h3><ul>{preview.guarantees.map((item) => <li key={item}>{item}</li>)}</ul>
      {preview.kind === "STRATEGIC" && <p className="notice">Система предложит подготовить новую версию стратегии. Медиаплан потребуется пересмотреть отдельным действием.</p>}
      <label htmlFor="change-comment">Причина или комментарий (необязательно)</label><textarea id="change-comment" value={comment} onChange={(event) => setComment(event.target.value)} />
      <div className="actions"><button disabled={saving} onClick={() => void save()}>{saving ? "Сохраняем…" : "Сохранить изменения"}</button><button className="secondary" onClick={() => { setPreview(null); setPendingValues(null); }}>Отмена</button></div>
    </section>}
  </div>;
}
