"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { CampaignForm } from "@/components/campaign-form";
import { campaignsApi, type Campaign, type CampaignInput } from "@/lib/api";

export default function EditCampaignPage() {
  const { id } = useParams<{ id: string }>(); const router = useRouter(); const { user, loading: authLoading } = useAuth();
  const [campaign, setCampaign] = useState<Campaign | null>(null); const [error, setError] = useState("");
  useEffect(() => { if (!authLoading && !user) { router.replace("/login"); return; } if (user) campaignsApi.get(id).then((value) => { if (value.status === "ARCHIVED") router.replace(`/campaigns/${id}`); else setCampaign(value); }).catch((reason: Error) => setError(reason.message)); }, [authLoading, user, id, router]);
  if (authLoading || (!campaign && !error)) return <main><p>Загружаем кампанию…</p></main>;
  if (!campaign) return <main><section><p role="alert" className="error">{error}</p><Link href="/campaigns">К кампаниям</Link></section></main>;
  const initial: CampaignInput = { name: campaign.name, description: campaign.description, goal: campaign.goal, product: campaign.product, target_audience: campaign.target_audience, offer: campaign.offer, desired_result: campaign.desired_result, start_date: campaign.start_date, end_date: campaign.end_date };
  async function update(values: CampaignInput) { await campaignsApi.update(id, values); router.push(`/campaigns/${id}`); }
  return <main><section className="wide"><header className="page-header"><div><p className="eyebrow">Редактирование</p><h1>{campaign.name}</h1></div><Link href={`/campaigns/${id}`}>Отмена</Link></header><CampaignForm initial={initial} submitLabel="Сохранить" onSubmit={update} /></section></main>;
}
