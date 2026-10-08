"use client";
import Link from "next/link";
import { useEffect } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";

export const campaignSections = [["", "Обзор"], ["strategy", "Стратегия"], ["content", "Контент"], ["plan", "Медиаплан"], ["publications", "Публикации"], ["performance", "Результаты"], ["tasks", "Задачи"], ["activity", "История"], ["director", "Директор"]] as const;
export function canonicalCampaignHref(id: string, hash: string): string | null {
  const base = `/campaigns/${id}`;
  if (hash === "#strategy") return `${base}/strategy`;
  if (hash === "#campaign-change") return `${base}/strategy${hash}`;
  if (hash === "#publication-plan") return `${base}/plan`;
  if (/^#(?:publication-plan-|plan-item-)/.test(hash)) return `${base}/plan${hash}`;
  if (hash === "#feedback") return `${base}/performance`;
  if (hash.startsWith("#experiment-")) return `${base}/performance${hash}`;
  return null;
}
export function LegacyCampaignRedirect() {
  const { id } = useParams<{ id: string }>(); const router = useRouter();
  useEffect(() => { const replace = () => { const href = canonicalCampaignHref(id, window.location.hash); if (href) router.replace(href); }; replace(); window.addEventListener("hashchange", replace); return () => window.removeEventListener("hashchange", replace); }, [id, router]);
  return null;
}
export function CampaignNavigation({ id }: { id: string }) {
  const pathname = usePathname();
  return <nav className="campaign-navigation" aria-label="Разделы кампании">{campaignSections.map(([path,label]) => { const href = `/campaigns/${id}${path ? `/${path}` : ""}`; return <Link key={path} href={href} aria-current={pathname === href ? "page" : undefined}>{label}</Link>; })}</nav>;
}
