"use client";
import { type CSSProperties, ReactNode, useEffect, useState } from "react";
import { useParams, usePathname } from "next/navigation";
import Link from "next/link";
import { CampaignNavigation, campaignSections } from "@/components/campaign-navigation";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { StatusBadge } from "@/components/status-badge";
import { campaignsApi, type Campaign } from "@/lib/api";
import { CAMPAIGN_STATUS_LABELS, formatDate } from "@/lib/campaigns";
export default function CampaignLayout({ children }: { children: ReactNode }) {
  const { id } = useParams<{ id: string }>(); const pathname = usePathname(); const [campaign,setCampaign] = useState<Campaign | null>(null);
  useEffect(() => { let active = true; const refresh = () => { void campaignsApi.get(id).then(c => { if (active) setCampaign(c); }).catch(() => undefined); }; const updated = (event: Event) => { if ((event as CustomEvent<string>).detail === id) refresh(); }; refresh(); window.addEventListener("campaign-workspace-updated", updated); return () => { active = false; window.removeEventListener("campaign-workspace-updated", updated); }; }, [id]);
  const [topbarHeight, setTopbarHeight] = useState(67);
  useEffect(() => {
    const topbar = document.querySelector(".app-topbar");
    if (!topbar) return;
    const measure = () => setTopbarHeight(topbar.getBoundingClientRect().height);
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(topbar);
    window.addEventListener("resize", measure);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); };
  }, []);
  const section = campaignSections.find(([path]) => pathname === `/campaigns/${id}${path ? `/${path}` : ""}`)?.[1] ?? "Редактирование";
  return <main className="wide campaign-shell" style={{"--campaign-topbar-height": `${topbarHeight}px`} as CSSProperties}><PageBreadcrumbs items={[{label:"Кампании",href:"/campaigns"},{label:campaign?.name ?? "Кампания",href:`/campaigns/${id}`},{label:section}]} /><header className="campaign-shell-header"><div><h1>{campaign?.name ?? "Кампания"}</h1><p className="compact-goal">{campaign?.goal}</p><p>{formatDate(campaign?.start_date ?? null)} — {formatDate(campaign?.end_date ?? null)}</p></div>{campaign && <StatusBadge label={CAMPAIGN_STATUS_LABELS[campaign.status]} />}{campaign?.status !== "ARCHIVED" && <Link href={`/campaigns/${id}/edit`}>Изменить вводные</Link>}</header><CampaignNavigation id={id} />{campaign?.status === "ARCHIVED" && <p className="notice">Архивная кампания: доступна для чтения.</p>}{children}</main>;
}
