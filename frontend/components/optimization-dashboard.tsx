"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { optimizationCategories, optimizationWorkspaceApi, type OptimizationDashboard } from "@/lib/optimization-workspace";
export function OptimizationDashboardBlock() {
 const [data,setData]=useState<OptimizationDashboard|null>(null); const [error,setError]=useState("");
 useEffect(()=>{let active=true;void optimizationWorkspaceApi.dashboard().then(value=>{if(active)setData(value);}).catch(e=>{if(active)setError(e instanceof Error ? e.message : "Результаты недоступны");});return ()=>{active=false;};},[]);
 return <section className="page-section" aria-labelledby="optimization-dashboard-heading"><h2 id="optimization-dashboard-heading">Результаты и оптимизация</h2>{error && <p role="alert">{error}</p>}{!data && !error && <p role="status">Загружаем результаты…</p>}{data && <><div className="summary-grid metric-cards">{Object.entries(optimizationCategories).map(([key,label])=>{const item=data.items.find(row=>row.category===key);return <div className="metric-card" key={key}><strong>{data.counts[key] ?? 0}</strong>{item ? <Link href={item.href}>{label}</Link> : <span>{label}</span>}</div>;})}</div>{data.items.length ? <ul>{data.items.map(item=><li key={`${item.campaign_id}-${item.category}`}><Link href={item.href}>{item.campaign_name} · {optimizationCategories[item.category]}: {item.count}</Link></li>)}</ul> : <p>Новых результатов и решений по оптимизации пока нет.</p>}</>}</section>;
}
