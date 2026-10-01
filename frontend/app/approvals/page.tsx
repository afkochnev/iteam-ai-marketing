"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { StatusBadge } from "@/components/status-badge";
import { approvalsApi, type Approval } from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";

const approvalStatus: Record<Approval["status"], string> = {
  PENDING: "Ожидает решения",
  APPROVED: "Утверждено",
  REJECTED: "Отклонено",
  REVISION_REQUESTED: "Нужна доработка",
};

export default function ApprovalsPage() {
  const [items, setItems] = useState<Approval[]>([]);
  const [error, setError] = useState("");
  useEffect(() => { void approvalsApi.list().then(setItems).catch((reason: Error) => setError(reason.message)); }, []);

  return <main className="page">
    <PageBreadcrumbs items={[{ label: "Согласования" }]} />
    <header className="page-header"><div><p className="eyebrow">Решения человека</p><h1>Согласования</h1><p className="page-subtitle">Проверяйте стратегию кампании и версии материалов до следующего этапа.</p></div><Link className="button-link secondary" href="/campaigns">К кампаниям</Link></header>
    {error && <div className="empty-state" role="alert"><h2>Не удалось загрузить согласования</h2><p>{error}</p></div>}
    {!error && !items.length && <p className="empty-state">Согласований пока нет.</p>}
    {!error && items.length > 0 && <div className="content-stack">{items.map((item) => {
      const isContent = item.object_type === "CONTENT_ITEM";
      return <article className="content-card" key={item.id}><div className="card-heading"><div><p className="eyebrow">{isContent ? "Материал" : "Стратегия кампании"} · версия {item.subject_version}</p><h2><Link href={isContent ? `/content/${item.object_id}` : `/campaigns/${item.object_id}`}>{isContent ? "Открыть материал" : "Открыть кампанию"}</Link></h2><p className="muted">Создано {formatDateTime(item.created_at)}</p></div><StatusBadge status={item.status} label={approvalStatus[item.status]} /></div>{item.comment && <p>{item.comment}</p>}</article>;
    })}</div>}
  </main>;
}
