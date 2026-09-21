"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { CampaignForm } from "@/components/campaign-form";
import { useAuth } from "@/components/auth-provider";
import { campaignsApi, type CampaignInput } from "@/lib/api";

export default function NewCampaignPage() {
  const { user, loading } = useAuth(); const router = useRouter();
  useEffect(() => { if (!loading && !user) router.replace("/login"); }, [loading, user, router]);
  if (loading || !user) return <main><p>Проверяем авторизацию…</p></main>;
  async function create(values: CampaignInput) { const campaign = await campaignsApi.create(values); router.push(`/campaigns/${campaign.id}`); }
  return <main><section className="wide"><header className="page-header"><div><p className="eyebrow">Новая</p><h1>Создать кампанию</h1></div><Link href="/campaigns">Отмена</Link></header><CampaignForm submitLabel="Создать кампанию" onSubmit={create} /></section></main>;
}
