"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { campaignsApi, type CampaignListItem, type CampaignStatus } from "@/lib/api";
import { CAMPAIGN_STATUS_LABELS, formatDate, formatDateTime } from "@/lib/campaigns";

export default function CampaignsPage() {
  const { user, loading: authLoading } = useAuth(); const router = useRouter();
  const [campaigns, setCampaigns] = useState<CampaignListItem[]>([]); const [filter, setFilter] = useState<CampaignStatus | "">("");
  const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  useEffect(() => { if (!authLoading && !user) { router.replace("/login"); return; } if (user) campaignsApi.list(filter || undefined).then(setCampaigns).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false)); }, [authLoading, user, router, filter]);
  if (authLoading) return <main><p>Проверяем авторизацию…</p></main>;
  return <main><section className="wide"><header className="page-header"><div><p className="eyebrow">Маркетинг</p><h1>Кампании</h1></div><div><Link href="/">На главную</Link> <Link className="button-link" href="/campaigns/new">Новая кампания</Link></div></header>
    <label htmlFor="status-filter">Статус</label><select id="status-filter" value={filter} onChange={(e) => { setLoading(true); setError(""); setFilter(e.target.value as CampaignStatus | ""); }}><option value="">Текущие кампании</option>{Object.entries(CAMPAIGN_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
    {loading ? <p>Загружаем кампании…</p> : error ? <p role="alert" className="error">{error}</p> : campaigns.length === 0 ? <div className="empty"><h2>Кампаний пока нет</h2><p>Создайте первую маркетинговую кампанию.</p></div> : <div className="campaign-list">{campaigns.map((campaign) => <Link href={`/campaigns/${campaign.id}`} className="campaign-row" key={campaign.id}><div><strong>{campaign.name}</strong><small>{campaign.product ?? "Продукт не указан"}</small></div><span>{CAMPAIGN_STATUS_LABELS[campaign.status]}</span><span>{campaign.goal}</span><span>{formatDate(campaign.start_date)} — {formatDate(campaign.end_date)}</span><span>{formatDateTime(campaign.created_at)}</span></Link>)}</div>}
  </section></main>;
}
