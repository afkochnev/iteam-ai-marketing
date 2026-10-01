"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { StatusBadge } from "@/components/status-badge";
import { useAuth } from "@/components/auth-provider";
import { campaignsApi, publicationsApi, type CampaignListItem, type PublicationCalendarItem } from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";
import { PUBLICATION_STATUS_LABELS } from "@/lib/presentation";

type CalendarRow = PublicationCalendarItem & { campaign: CampaignListItem };

export default function PublicationsPage() {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [rows, setRows] = useState<CalendarRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!authLoading && !user) { router.replace("/login"); return; }
    if (!user) return;
    const to = new Date();
    const from = new Date(to.getTime() - 30 * 24 * 60 * 60 * 1000);
    const end = new Date(to.getTime() + 90 * 24 * 60 * 60 * 1000);
    campaignsApi.list().then(async (campaigns) => {
      const results = await Promise.all(campaigns.map(async (campaign) => {
        const items = await publicationsApi.calendar(campaign.id, from.toISOString(), end.toISOString());
        return items.map((item) => ({ ...item, campaign }));
      }));
      setRows(results.flat());
    }).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false));
  }, [authLoading, user, router]);

  const upcoming = useMemo(() => rows.filter((item) => item.status === "SCHEDULED" && item.scheduled_at)
    .sort((a, b) => new Date(a.scheduled_at ?? 0).getTime() - new Date(b.scheduled_at ?? 0).getTime()), [rows]);
  const published = useMemo(() => rows.filter((item) => item.status === "PUBLISHED")
    .sort((a, b) => new Date(b.published_at ?? 0).getTime() - new Date(a.published_at ?? 0).getTime()), [rows]);
  const attention = useMemo(() => rows.filter((item) => item.status === "FAILED" || item.failure_code), [rows]);

  if (authLoading) return <main><p role="status">Проверяем авторизацию…</p></main>;
  return <main className="page">
    <PageBreadcrumbs items={[{ label: "Публикации" }]} />
    <header className="page-header"><div><p className="eyebrow">Расписание и факт доставки</p><h1>Публикации</h1><p className="page-subtitle">Запланированные публикации и история фактической отправки по кампаниям. Время показано в часовом поясе браузера.</p></div><Link className="button-link secondary" href="/campaigns">К кампаниям</Link></header>
    <div className="summary-grid metric-cards"><a className="metric-card" href="#upcoming"><strong>{upcoming.length}</strong><span>Запланировано</span></a><a className="metric-card" href="#published"><strong>{published.length}</strong><span>Опубликовано</span></a><a className="metric-card" href="#attention"><strong>{attention.length}</strong><span>Требуют проверки</span></a></div>
    {loading ? <p role="status">Загружаем календарь…</p> : error ? <div className="empty-state" role="alert"><h2>Не удалось загрузить календарь</h2><p>{error}</p><button onClick={() => window.location.reload()}>Повторить</button></div> : <>
      <section className="section-block" id="upcoming"><div className="section-heading"><div><p className="eyebrow">План</p><h2>Предстоящие</h2></div></div>
        {upcoming.length ? <div className="content-stack">{upcoming.map((item) => <article className="content-card" key={item.publication_id}><div className="card-heading"><div><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel === "VK" ? "VK" : "Telegram"} · {formatDateTime(item.scheduled_at ?? "")}</span></p></div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div></article>)}</div> : <p className="empty-state">Запланированных публикаций пока нет.</p>}
      </section>
      <section className="section-block" id="attention"><div className="section-heading"><div><p className="eyebrow">Операторская проверка</p><h2>Ошибки отправки</h2></div></div>
        {attention.length ? <div className="content-stack">{attention.map((item) => <article className="content-card" key={item.publication_id}><div className="card-heading"><div><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p>{item.campaign.name} · {item.failure_code ?? "Статус требует проверки"}</p></div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div></article>)}</div> : <p className="empty-state">Ошибок отправки нет.</p>}
      </section>
      <details className="disclosure" id="published"><summary>Опубликованные · {published.length}</summary><div className="disclosure-body">{published.length ? <div className="content-stack">{published.map((item) => <article className="content-card" key={item.publication_id}><div className="card-heading"><div><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p>{item.campaign.name} · {item.channel === "VK" ? "VK" : "Telegram"}{item.published_at ? ` · ${formatDateTime(item.published_at)}` : ""}</p></div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>{item.external_url && <a href={item.external_url} target="_blank" rel="noreferrer">Открыть публикацию</a>}</article>)}</div> : <p>История публикаций пока пуста.</p>}</div></details>
    </>}
  </main>;
}
