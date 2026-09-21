"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { campaignsApi, tasksApi, type Campaign, type TaskListItem } from "@/lib/api";
import { CAMPAIGN_STATUS_LABELS, formatDate, formatDateTime } from "@/lib/campaigns";
import { TASK_STATUS_LABELS } from "@/lib/tasks";

export default function CampaignDetailsPage() {
  const { id } = useParams<{ id: string }>(); const router = useRouter(); const { user, loading: authLoading } = useAuth();
  const [campaign, setCampaign] = useState<Campaign | null>(null); const [error, setError] = useState("");
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  useEffect(() => { if (!authLoading && !user) { router.replace("/login"); return; } if (user) Promise.all([campaignsApi.get(id), tasksApi.list({ campaign_id: id })]).then(([campaignValue, taskRows]) => { setCampaign(campaignValue); setTasks(taskRows); }).catch((reason: Error) => setError(reason.message)); }, [authLoading, user, id, router]);
  async function archive() { if (!window.confirm("Архивировать кампанию? После архивации редактирование будет недоступно.")) return; try { setCampaign(await campaignsApi.archive(id)); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось архивировать кампанию."); } }
  if (authLoading || (!campaign && !error)) return <main><p>Загружаем кампанию…</p></main>;
  if (!campaign) return <main><section><p role="alert" className="error">{error}</p><Link href="/campaigns">К кампаниям</Link></section></main>;
  const archived = campaign.status === "ARCHIVED";
  return <main><section className="wide"><header className="page-header"><div><p className="eyebrow">Обзор</p><h1>{campaign.name}</h1><p className="status-text">{CAMPAIGN_STATUS_LABELS[campaign.status]}</p></div><Link href="/campaigns">К кампаниям</Link></header>
    {error && <p role="alert" className="error">{error}</p>}<div className="details-grid"><div><dt>Цель</dt><dd>{campaign.goal}</dd></div><div><dt>Продукт</dt><dd>{campaign.product ?? "Не указан"}</dd></div><div><dt>Целевая аудитория</dt><dd>{campaign.target_audience ?? "Не указана"}</dd></div><div><dt>Оффер</dt><dd>{campaign.offer ?? "Не указан"}</dd></div><div><dt>Желаемый результат</dt><dd>{campaign.desired_result ?? "Не указан"}</dd></div><div><dt>Период</dt><dd>{formatDate(campaign.start_date)} — {formatDate(campaign.end_date)}</dd></div><div><dt>Описание</dt><dd>{campaign.description ?? "Нет дополнительного контекста"}</dd></div><div><dt>Кто создал</dt><dd>{campaign.creator.full_name ?? campaign.creator.email}</dd></div><div><dt>Создана</dt><dd>{formatDateTime(campaign.created_at)}</dd></div><div><dt>Изменена</dt><dd>{formatDateTime(campaign.updated_at)}</dd></div></div>
    {!archived && <div className="actions"><Link className="button-link" href={`/campaigns/${id}/edit`}>Редактировать</Link><button className="secondary" onClick={archive}>Архивировать</button></div>}
    <div className="page-header"><h2>Задачи</h2>{!archived && <Link className="button-link" href={`/tasks/new?campaign_id=${id}`}>Создать задачу</Link>}</div>
    <div className="summary-grid"><div><strong>{tasks.length}</strong><span>Всего</span></div><div><strong>{tasks.filter((item) => item.status === "READY").length}</strong><span>Ready</span></div><div><strong>{tasks.filter((item) => item.status === "BLOCKED").length}</strong><span>Blocked</span></div><div><strong>{tasks.filter((item) => item.status === "IN_PROGRESS").length}</strong><span>In Progress</span></div><div><strong>{tasks.filter((item) => item.status === "COMPLETED").length}</strong><span>Completed</span></div></div>
    {tasks.length ? <ul>{tasks.map((task) => <li key={task.id}><Link href={`/tasks/${task.id}`}>{task.title}</Link> — {TASK_STATUS_LABELS[task.status]}</li>)}</ul> : <p>У кампании пока нет задач.</p>}
  </section></main>;
}
