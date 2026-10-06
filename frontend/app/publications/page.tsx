"use client";

import Link from "@/components/hash-link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { PublicationDeliveryNotice } from "@/components/publication-delivery-notice";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { StatusBadge } from "@/components/status-badge";
import { useAuth } from "@/components/auth-provider";
import { useHashTarget } from "@/components/use-hash-target";
import { campaignsApi, contentApi, publicationPlansApi, publicationsApi, type CampaignListItem, type ContentListItem, type Publication, type PublicationPlan, type PublicationCalendarItem } from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";
import { PUBLICATION_STATUS_LABELS } from "@/lib/presentation";

type CalendarRow = Omit<PublicationCalendarItem, "provider_enabled"> & { provider_enabled?: boolean; campaign: CampaignListItem };
type Candidate = ContentListItem & { campaign: CampaignListItem; planApproved: boolean };

type ReadData = { calendar: PublicationCalendarItem[]; publications: Publication[]; content: ContentListItem[]; plans: PublicationPlan[] };
type ReadKind = keyof ReadData;
type ReadFailure = { campaign: CampaignListItem; kind: ReadKind };
const readLabels: Record<ReadKind, string> = { calendar: "Календарь", publications: "Публикации", content: "Материалы", plans: "Медиапланы" };

export default function PublicationsPage() {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [rows, setRows] = useState<CalendarRow[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const cache = useRef(new Map<string, Partial<ReadData>>());
  const [failures, setFailures] = useState<ReadFailure[]>([]);
  useHashTarget(rows);
  const load = useCallback(async () => {
    setLoading(true); setError("");
    const from = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000);
    const end = new Date(from.getTime() + 90 * 24 * 60 * 60 * 1000);
    try {
      const campaigns = await campaignsApi.list();
      const results = await Promise.all(campaigns.map(async (campaign) => {
        const settled = await Promise.allSettled([
          publicationsApi.calendar(campaign.id, from.toISOString(), end.toISOString()),
          publicationsApi.listCampaign(campaign.id),
          contentApi.list({ campaign_id: campaign.id, content_type: "SOCIAL_POST" }),
          publicationPlansApi.list(campaign.id),
        ]);
        const kinds: ReadKind[] = ["calendar", "publications", "content", "plans"];
        const previous = cache.current.get(campaign.id) ?? {};
        const data = { ...previous };
        const failed: ReadFailure[] = [];
        settled.forEach((result, index) => {
          const kind = kinds[index];
          if (result.status === "fulfilled") Object.assign(data, { [kind]: result.value });
          else failed.push({ campaign, kind });
        });
        cache.current.set(campaign.id, data);
        const { calendar = [], publications = [], content = [], plans = [] } = data;
        // Include undated failures and scheduled rows beyond the calendar window.
        const all = new Map<string, CalendarRow>(calendar.map((item) => [item.publication_id, { ...item, campaign }]));
        for (const publication of publications) {
          // A failed list refresh must not overwrite a fresh calendar row with cached data.
          if (settled[1].status === "rejected" && settled[0].status === "fulfilled" && all.has(publication.id)) continue;
          all.set(publication.id, {
          publication_id: publication.id, content_item_id: publication.content_item_id,
          content_version_id: publication.content_version_id, title: content.find((item) => item.id === publication.content_item_id)?.title ?? all.get(publication.id)?.title ?? "Открыть материал",
          channel: publication.channel, status: publication.status, scheduled_at: publication.scheduled_at,
          published_at: publication.published_at, external_url: publication.external_url,
          provider_enabled: publication.provider_enabled, failure_code: publication.failure_code, is_overdue: publication.is_overdue, lateness_seconds: publication.lateness_seconds, campaign,
          });
        }
        const ready = campaign.status === "ARCHIVED" || failed.some((failure) => failure.kind !== "calendar") ? [] : content.filter((item) =>
          item.content_type === "SOCIAL_POST" && item.status === "APPROVED" && item.approved_version_id &&
          item.approved_version_id === item.current_version_id &&
          ![...all.values()].some((publication) => publication.content_item_id === item.id && publication.status !== "CANCELLED")
        ).map((item) => ({ ...item, campaign, planApproved: plans.some((plan) => plan.status === "APPROVED" && plan.items.some((planned) => planned.id === item.publication_plan_item_id && planned.status === "PLANNED")) }));
        return { rows: [...all.values()], candidates: ready, failed };
      }));
      setFailures(results.flatMap((result) => result.failed));
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
  const upcoming = rows.filter((item) => item.status === "SCHEDULED" && item.scheduled_at && !item.is_overdue)
    .sort((a, b) => Date.parse(a.scheduled_at!) - Date.parse(b.scheduled_at!));
  const overdue = rows.filter((item) => item.status === "SCHEDULED" && item.is_overdue);
  const active = rows.filter((item) => ["DRAFT", "WAITING_APPROVAL", "APPROVED", "PUBLISHING"].includes(item.status));
  const published = rows.filter((item) => item.status === "PUBLISHED");
  const attention = rows.filter((item) => item.status !== "CANCELLED" && (item.status === "FAILED" || item.failure_code));
  const ready = candidates.filter((item) => item.planApproved);
  const needsPlan = candidates.filter((item) => !item.planApproved);
  const readyIncomplete = failures.some((failure) => failure.kind !== "calendar");
  const rowsIncomplete = failures.some((failure) => failure.kind === "publications" || failure.kind === "calendar");
  const incomplete = <p className="notice">Данные загружены не полностью. Отсутствие записей пока не подтверждено.</p>;
  const version = (item: CalendarRow) => <span>Версия публикации: <code>{item.content_version_id}</code></span>;
  const delivery = (item: CalendarRow) => <PublicationDeliveryNotice providerEnabled={item.provider_enabled} />;
  if (authLoading) return <main><p role="status">Проверяем авторизацию…</p></main>;
  return <main className="page">
    <PageBreadcrumbs items={[{ label: "Публикации" }]} />
    <header className="page-header"><div><p className="eyebrow">Расписание и факт доставки</p><h1>Публикации</h1><p className="page-subtitle">Утверждённые посты, расписание и история отправки. Время показано в часовом поясе браузера.</p></div><Link className="button-link secondary" href="/campaigns">К кампаниям</Link></header>
    <p className="notice">Планирование не означает отправку. Доставка требует отдельного разрешения человека и доступного publishing runtime.</p>
    <div className="summary-grid metric-cards"><a className="metric-card" href="#ready"><strong>{readyIncomplete ? "—" : ready.length}</strong><span>Готовы к планированию</span></a><a className="metric-card" href="#active"><strong>{rowsIncomplete ? "—" : active.length}</strong><span>Требуют действия / В процессе</span></a><a className="metric-card" href="#upcoming"><strong>{rowsIncomplete ? "—" : upcoming.length}</strong><span>Предстоящие</span></a><a className="metric-card" href="#overdue"><strong>{rowsIncomplete ? "—" : overdue.length}</strong><span>Просрочено</span></a><a className="metric-card" href="#published"><strong>{rowsIncomplete ? "—" : published.length}</strong><span>Опубликовано</span></a><a className="metric-card" href="#attention"><strong>{rowsIncomplete ? "—" : attention.length}</strong><span>Требуют проверки</span></a></div>
    {loading && <p role="status">Загружаем календарь…</p>}
    {error ? <div className="empty-state" role="alert"><h2>Не удалось загрузить календарь</h2><p>{error}</p><button onClick={() => void load()}>Повторить</button> <Link href="/content">Открыть контент</Link></div> : <>
      {failures.map((failure) => <div className="notice" role="alert" key={`${failure.campaign.id}-${failure.kind}`}>
        <strong>{readLabels[failure.kind]} · {failure.campaign.name}</strong>
        <p>Не удалось обновить этот раздел. Успешно загруженные данные сохранены; список может быть неполным.</p>
        <button disabled={loading} onClick={() => void load()}>Повторить загрузку: {readLabels[failure.kind]}</button>
      </div>)}
      <section className="section-block" id="ready"><h2>Готовы к планированию</h2>{ready.length ? <div className="content-stack">{ready.map((item) => <article className="content-card" key={item.id}><h3><Link href={`/content/${item.id}`}>{item.title}</Link></h3><p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.plan_channel ?? item.channel} · утверждена версия v{item.approved_version_number ?? item.current_version_number}{item.plan_scheduled_at ? ` · ${formatDateTime(item.plan_scheduled_at)}` : ""}</p><Link className="button-link" href={`/content/${item.id}#publication`}>Открыть пост и запланировать</Link></article>)}</div> : readyIncomplete || loading ? incomplete : <p className="empty-state">Готовых к планированию постов пока нет. <Link href="/content">Проверить контент и согласования</Link></p>}
        {needsPlan.length > 0 && <div><h3>Утверждённые посты без утверждённого пункта плана</h3>{needsPlan.map((item) => <p key={item.id}><Link href={`/content/${item.id}`}>{item.title}</Link> · <Link href={`/campaigns/${item.campaign.id}#publication-plan`}>Открыть кампанию и проверить медиаплан</Link></p>)}</div>}
      </section>
      <section className="section-block" id="overdue" aria-labelledby="overdue-heading">
        <h2 id="overdue-heading">Просрочены / Требуют решения</h2>
        {overdue.length ? <div className="content-stack">{overdue.map((item) => <article className="content-card" key={item.publication_id}>
          <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
          <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.channel} · Плановое время: {item.scheduled_at ? formatDateTime(item.scheduled_at) : "—"}</p>
          {version(item)}
          <p>OVERDUE · Просрочка: {Math.ceil((item.lateness_seconds ?? 0) / 60)} мин.</p>
          <p role="alert">Плановое время прошло; автоматическая отправка остановлена до решения.</p>
          {delivery(item)}
          <Link href={`/campaigns/${item.campaign.id}#publication-${item.publication_id}`}>Опубликовать сейчас / Отменить — открыть решение оператора</Link>
          <p>Перенос публикации из медиаплана требует изменения медиаплана. Для публикации без привязки доступно назначение нового времени в кампании.</p>
          <Link href={`/campaigns/${item.campaign.id}#publication-plan`}>Проверить медиаплан и расписание</Link>
        </article>)}</div> : rowsIncomplete || loading ? incomplete : <p>Просроченных публикаций нет.</p>}
      </section>
      <section className="section-block" id="active" aria-labelledby="active-heading">
        <h2 id="active-heading">Требуют действия / В процессе</h2>
        {active.length ? <div className="content-stack">{active.map((item) => <article className="content-card" key={item.publication_id}>
          <div className="card-heading"><div>
            <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
            <p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel}{item.scheduled_at ? ` · ${formatDateTime(item.scheduled_at)}` : ""}</span>{version(item)}</p>
          </div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>
          {delivery(item)}
          {item.status === "PUBLISHING" ? <>
            <p role="status">Отправка выполняется / результат ещё не подтверждён. Повторная отправка недоступна.</p>
            <Link href={`/campaigns/${item.campaign.id}#publications`}>Проверить состояние отправки в кампании</Link>
          </> : <Link href={`/campaigns/${item.campaign.id}#publications`}>
            {item.status === "APPROVED" ? "Открыть кампанию и назначить публикацию" : "Открыть кампанию и согласовать публикацию"}
          </Link>}
        </article>)}</div> : rowsIncomplete || loading ? incomplete : <p className="empty-state">Публикаций, требующих действия или находящихся в процессе отправки, нет.</p>}
      </section>
      <section className="section-block" id="upcoming"><h2>Предстоящие</h2>{upcoming.length ? <div className="content-stack">{upcoming.map((item) => <article className="content-card" key={item.publication_id}><div className="card-heading"><div><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel === "VK" ? "VK" : "Telegram"} · {formatDateTime(item.scheduled_at!)}</span>{version(item)}</p></div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>{delivery(item)}<Link href={`/content/${item.content_item_id}#publication`}>Проверить пост и запланированную версию</Link></article>)}</div> : rowsIncomplete || loading ? incomplete : <p className="empty-state">Запланированных публикаций пока нет. <a href="#ready">Проверить готовые посты</a></p>}</section>
      <section className="section-block" id="attention"><h2>Ошибки отправки</h2>{attention.length ? <div className="content-stack">{attention.map((item) => <article className="content-card" key={item.publication_id}><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.failure_code ?? "Статус требует проверки"}</p>{version(item)}<StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} />{delivery(item)}<Link href={`/campaigns/${item.campaign.id}#publications`}>Открыть кампанию: повтор или проверка результата отправки</Link></article>)}</div> : rowsIncomplete || loading ? incomplete : <p className="empty-state">Ошибок отправки нет.</p>}</section>
      <details className="disclosure" id="published"><summary>Опубликованные · {rowsIncomplete ? "неполные данные" : published.length}</summary><div className="disclosure-body">{published.length ? published.map((item) => <article className="content-card" key={item.publication_id}><h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3><p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.channel}{item.published_at ? ` · ${formatDateTime(item.published_at)}` : ""}</p>{version(item)}<StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} />{item.external_url && <a href={item.external_url} target="_blank" rel="noreferrer">Открыть публикацию</a>}</article>) : rowsIncomplete || loading ? incomplete : <p>История публикаций пока пуста.</p>}</div></details>
    </>}
  </main>;
}
