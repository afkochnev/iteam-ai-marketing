"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { StatusBadge } from "@/components/status-badge";
import { useAuth } from "@/components/auth-provider";
import {
  campaignsApi,
  contentApi,
  type CampaignListItem,
  type ContentListItem,
  type ContentStatus,
  type ContentType,
} from "@/lib/api";
import { CONTENT_STATUS_LABELS, CONTENT_TYPE_LABELS } from "@/lib/presentation";
import { formatDateTime } from "@/lib/campaigns";

export default function ContentPage() {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [items, setItems] = useState<ContentListItem[]>([]);
  const [campaigns, setCampaigns] = useState<CampaignListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [type, setType] = useState<ContentType | "">("");
  const [status, setStatus] = useState<ContentStatus | "">("");
  const [campaignId, setCampaignId] = useState("");
  const [channel, setChannel] = useState("");
  const [sort, setSort] = useState<"newest" | "oldest">("newest");

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams(window.location.search);
      const requestedType = params.get("content_type");
      const requestedStatus = params.get("status");
      if (requestedType === "ARTICLE" || requestedType === "SOCIAL_POST" || requestedType === "SOCIAL_POST_PACK") setType(requestedType);
      if (requestedStatus === "DRAFT" || requestedStatus === "WAITING_REVIEW" || requestedStatus === "WAITING_APPROVAL" || requestedStatus === "APPROVED" || requestedStatus === "REJECTED" || requestedStatus === "ARCHIVED") setStatus(requestedStatus);
      setCampaignId(params.get("campaign_id") ?? "");
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/login");
      return;
    }
    if (!user) return;
    Promise.all([contentApi.list(), campaignsApi.list()])
      .then(([contentRows, campaignRows]) => {
        setItems(contentRows);
        setCampaigns(campaignRows);
        setError("");
      })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false));
  }, [authLoading, user, router]);

  const campaignNames = useMemo(
    () => new Map(campaigns.map((campaign) => [campaign.id, campaign.name])),
    [campaigns],
  );
  const filtered = useMemo(() => items
    .filter((item) => !type || item.content_type === type)
    .filter((item) => !status || item.status === status)
    .filter((item) => !campaignId || item.campaign_id === campaignId)
    .filter((item) => !channel || item.channel === channel)
    .filter((item) => `${item.title} ${item.source_content_item_title ?? ""}`.toLocaleLowerCase("ru-RU").includes(search.trim().toLocaleLowerCase("ru-RU")))
    .sort((a, b) => sort === "newest"
      ? new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
      : new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime()),
  [items, type, status, campaignId, channel, search, sort]);

  const clearFilters = () => {
    setSearch(""); setType(""); setStatus(""); setCampaignId(""); setChannel(""); setSort("newest");
  };

  if (authLoading) return <main><p>Проверяем авторизацию…</p></main>;
  return (
    <main className="page">
      <PageBreadcrumbs items={[{ label: "Контент" }]} />
      <header className="page-header">
        <div><p className="eyebrow">Рабочая библиотека</p><h1>Контент</h1><p className="page-subtitle">Статьи, социальные посты и их происхождение. Откройте материал, чтобы посмотреть версии, согласования и источники.</p></div>
        <span className="subtle">Найдено: {filtered.length}</span>
      </header>

      <section className="section-block" aria-label="Фильтры контента">
        <div className="filters">
          <div className="search-field"><label htmlFor="content-search">Поиск по названию</label><input id="content-search" type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Например, стратегическая сессия" /></div>
          <div><label htmlFor="content-type">Тип материала</label><select id="content-type" value={type} onChange={(event) => setType(event.target.value as ContentType | "")}><option value="">Все типы</option>{Object.entries(CONTENT_TYPE_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
          <div><label htmlFor="content-status">Статус</label><select id="content-status" value={status} onChange={(event) => setStatus(event.target.value as ContentStatus | "")}><option value="">Все статусы</option>{Object.entries(CONTENT_STATUS_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
          <div><label htmlFor="content-campaign">Кампания</label><select id="content-campaign" value={campaignId} onChange={(event) => setCampaignId(event.target.value)}><option value="">Все кампании</option>{campaigns.map((campaign) => <option key={campaign.id} value={campaign.id}>{campaign.name}</option>)}</select></div>
          <div><label htmlFor="content-channel">Канал поста</label><select id="content-channel" value={channel} onChange={(event) => setChannel(event.target.value)}><option value="">Все каналы</option><option value="TELEGRAM">Telegram</option><option value="VK">VK</option></select></div>
          <div><label htmlFor="content-sort">Сортировка</label><select id="content-sort" value={sort} onChange={(event) => setSort(event.target.value as "newest" | "oldest")}><option value="newest">Сначала недавно изменённые</option><option value="oldest">Сначала давно изменённые</option></select></div>
        </div>
        <button className="secondary" onClick={clearFilters}>Сбросить фильтры</button>
      </section>

      {loading ? <p role="status">Загружаем контент…</p> : error ? <div className="empty-state" role="alert"><h2>Не удалось загрузить контент</h2><p>{error}</p><button className="secondary" onClick={() => window.location.reload()}>Повторить</button></div> : filtered.length === 0 ? (
        <div className="empty-state"><h2>{items.length ? "По этим фильтрам ничего нет" : "Контента пока нет"}</h2><p>{items.length ? "Измените поиск или сбросьте фильтры." : "Созданные статьи и посты кампаний появятся здесь."}</p>{items.length > 0 && <button className="secondary" onClick={clearFilters}>Сбросить фильтры</button>}</div>
      ) : (
        <div className="content-stack" aria-label="Список материалов">
          {filtered.map((item) => (
            <article className="content-row" key={item.id}>
              <div className="content-row-main">
                <div className="row-meta"><span className="type-label">{CONTENT_TYPE_LABELS[item.content_type]}</span><StatusBadge label={CONTENT_STATUS_LABELS[item.status]} status={item.status} /></div>
                <Link className="content-row-title" href={`/content/${item.id}`}>{item.title}</Link>
                <div className="row-meta"><Link href={`/campaigns/${item.campaign_id}`}>{campaignNames.get(item.campaign_id) ?? "Кампания"}</Link><span>Текущая версия: {item.current_version_number ?? "не создана"}</span><span>Утверждённая версия: {item.approved_version_number ? `v${item.approved_version_number}` : "нет"}</span><span>Изменён: {formatDateTime(item.updated_at)}</span></div>
                {(item.source_content_item_title || item.parent_content_item_id) && <div className="row-meta"><span>Источник: {item.source_content_item_title ?? "пакет публикаций"}</span></div>}
                {item.content_type === "SOCIAL_POST" && <div className="row-meta"><span>Канал: {item.plan_channel ?? item.channel ?? "не указан"}</span>{item.plan_scheduled_at && <span>По плану: {formatDateTime(item.plan_scheduled_at)}</span>}</div>}
              </div>
              <Link className="inline-link" href={`/content/${item.id}`}>Открыть материал →</Link>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
