"use client";

import Link from "@/components/hash-link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { PublicationDeliveryNotice } from "@/components/publication-delivery-notice";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { StatusBadge } from "@/components/status-badge";
import { useAuth } from "@/components/auth-provider";
import { useHashTarget } from "@/components/use-hash-target";
import { campaignsApi, contentApi, publicationPlansApi, publicationsApi, type CampaignListItem, type ContentListItem, type PublicationCalendarItem } from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";
import { PUBLICATION_STATUS_LABELS } from "@/lib/presentation";

type CalendarRow = Omit<PublicationCalendarItem, "provider_enabled"> & { provider_enabled?: boolean; campaign: CampaignListItem };
type Candidate = ContentListItem & { campaign: CampaignListItem; planApproved: boolean };

export default function PublicationsPage() {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [rows, setRows] = useState<CalendarRow[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useHashTarget(rows);
  const load = useCallback(async () => {
    setLoading(true); setError("");
    const from = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000);
    const end = new Date(from.getTime() + 90 * 24 * 60 * 60 * 1000);
    try {
      const campaigns = await campaignsApi.list();
      const results = await Promise.all(campaigns.map(async (campaign) => {
        const [calendar, publications, content, plans] = await Promise.all([
          publicationsApi.calendar(campaign.id, from.toISOString(), end.toISOString()),
          publicationsApi.listCampaign(campaign.id),
          contentApi.list({ campaign_id: campaign.id, content_type: "SOCIAL_POST" }),
          publicationPlansApi.list(campaign.id),
        ]);
        // Include undated failures and scheduled rows beyond the calendar window.
        const all = new Map<string, CalendarRow>(calendar.map((item) => [item.publication_id, { ...item, campaign }]));
        for (const publication of publications) all.set(publication.id, {
          publication_id: publication.id, content_item_id: publication.content_item_id,
          content_version_id: publication.content_version_id, title: content.find((item) => item.id === publication.content_item_id)?.title ?? "Открыть материал",
          channel: publication.channel, status: publication.status, scheduled_at: publication.scheduled_at,
          published_at: publication.published_at, external_url: publication.external_url,
          provider_enabled: publication.provider_enabled, failure_code: publication.failure_code, campaign,
        });
        const ready = campaign.status === "ARCHIVED" ? [] : content.filter((item) =>
          item.content_type === "SOCIAL_POST" && item.status === "APPROVED" && item.approved_version_id &&
          item.approved_version_id === item.current_version_id &&
          ![...all.values()].some((publication) => publication.content_item_id === item.id && publication.status !== "CANCELLED")
        ).map((item) => ({ ...item, campaign, planApproved: plans.some((plan) => plan.status === "APPROVED" && plan.items.some((planned) => planned.id === item.publication_plan_item_id && planned.status === "PLANNED")) }));
        return { rows: [...all.values()], candidates: ready };
      }));
      setRows(results.flatMap((result) => result.rows));
      setCandidates(results.flatMap((result) => result.candidates));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось загрузить публикации."); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    if (!authLoading && !user) { router.replace("/login"); return; }
    if (!user) return;
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [authLoading, user, router, load]);
  const upcoming = rows.filter((item) => item.status === "SCHEDULED" && item.scheduled_at)
    .sort((a, b) => Date.parse(a.scheduled_at!) - Date.parse(b.scheduled_at!));
  const published = rows.filter((item) => item.status === "PUBLISHED");
  const attention = rows.filter((item) => item.status !== "CANCELLED" && (item.status === "FAILED" || item.failure_code));
  const ready = candidates.filter((item) => item.planApproved);
  const needsPlan = candidates.filter((item) => !item.planApproved);
  const version = (item: CalendarRow) => <span>Версия публикации: <code>{item.content_version_id}</code></span>;
  const delivery = (item: CalendarRow) => <PublicationDeliveryNotice providerEnabled={item.provider_enabled} />;
  if (authLoading) return <main><p role="status">Проверяем авторизацию…</p></main>;
  return <main className="page">
    <PageBreadcrumbs items={[{ label: "Публикации" }]} />
    <header className="page-header"><div><p className="eyebrow">Расписание и факт доставки</p><h1>Публикации</h1><p className="page-subtitle">Утверждённые посты, расписание и история отправки. Время показано в часовом поясе браузера.</p></div><Link className="button-link secondary" href="/campaigns">К кампаниям</Link></header>
    <p className="notice">Планирование не означает отправку. Доставка требует отдельного разрешения человека и доступного publishing runtime.</p>
    <div className="summary-grid metric-cards"><a className="metric-card" href="#ready"><strong>{ready.length}</strong><span>Готовы к планированию</span></a><a className="metric-card" href="#upcoming"><strong>{upcoming.length}</strong><span>Запланировано</span></a><a className="metric-card" href="#published"><strong>{published.length}</strong><span>Опубликовано</span></a><a className="metric-card" href="#attention"><strong>{attention.length}</strong><span>Требуют проверки</span></a></div>
    {loading ? <p role="status">Загружаем календарь…</p> : error ? <div className="empty-state" role="alert"><h2>Не удалось загрузить календарь</h2><p>{error}</p><button onClick={() => void load()}>Повторить</button> <Link href="/content">Открыть контент</Link></div> : <>
      <section className="section-block" id="ready"><h2>Готовы к планированию</h2>{ready.length ? <div className="content-stack">{ready.map((item) => <article className="content-card" key={item.id}><h3><Link href={`/content/${item.id}`}>{item.title}</Link></h3><p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.plan_channel ?? item.channel} · утверждена версия v{item.approved_version_number ?? item.current_version_number}{item.plan_scheduled_at ? ` · ${formatDateTime(item.plan_scheduled_at)}` : ""}</p><Link className="button-link" href={`/content/${item.id}#publication`}>Открыть пост и запланировать</Link></article>)}</div> : <p className="empty-state">Готовых к планированию постов пока нет. <Link href="/content">Проверить контент и согласования</Link></p>}
        {needsPlan.length > 0 && <div><h3>Утверждённые посты без утверждённого пункта плана</h3>{needsPlan.map((item) => <p key={item.id}><Link href={`/content/${item.id}`}>{item.title}</Link> · <Link href={`/campaigns/${item.campaign.id}#publication-plan`}>Открыть кампанию и проверить медиаплан</Link></p>)}</div>}
      </section>
      <section className="section-block" id="upcoming"><h2>Предстоящие</h2>{upcoming.length ? <div className="content-stack">{upcoming.map((item) => <article className="content-card" key={item.publication_id}><div className="card-heading"><div><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel === "VK" ? "VK" : "Telegram"} · {formatDateTime(item.scheduled_at!)}</span>{version(item)}</p></div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>{delivery(item)}<Link href={`/content/${item.content_item_id}#publication`}>Проверить пост и запланированную версию</Link></article>)}</div> : <p className="empty-state">Запланированных публикаций пока нет. <a href="#ready">Проверить готовые посты</a></p>}</section>
      <section className="section-block" id="attention"><h2>Ошибки отправки</h2>{attention.length ? <div className="content-stack">{attention.map((item) => <article className="content-card" key={item.publication_id}><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.failure_code ?? "Статус требует проверки"}</p>{version(item)}<StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} />{delivery(item)}<Link href={`/campaigns/${item.campaign.id}#publications`}>Открыть кампанию: повтор или проверка результата отправки</Link></article>)}</div> : <p className="empty-state">Ошибок отправки нет.</p>}</section>
      <details className="disclosure" id="published"><summary>Опубликованные · {published.length}</summary><div className="disclosure-body">{published.length ? published.map((item) => <article className="content-card" key={item.publication_id}><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.channel}{item.published_at ? ` · ${formatDateTime(item.published_at)}` : ""}</p>{version(item)}<StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} />{item.external_url && <a href={item.external_url} target="_blank" rel="noreferrer">Открыть публикацию</a>}</article>) : <p>История публикаций пока пуста.</p>}</div></details>
    </>}
  </main>;
}
