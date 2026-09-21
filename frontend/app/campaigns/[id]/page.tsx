"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { approvalsApi, campaignsApi, tasksApi, type Approval, type Campaign, type TaskListItem } from "@/lib/api";
import { CAMPAIGN_STATUS_LABELS, formatDate, formatDateTime } from "@/lib/campaigns";
import { TASK_STATUS_LABELS } from "@/lib/tasks";

export default function CampaignDetailsPage() {
  const { id } = useParams<{ id: string }>(); const router = useRouter(); const { user, loading: authLoading } = useAuth();
  const [campaign, setCampaign] = useState<Campaign | null>(null); const [error, setError] = useState("");
  const [tasks, setTasks] = useState<TaskListItem[]>([]); const [approvals, setApprovals] = useState<Approval[]>([]); const [action, setAction] = useState<"revision" | "reject" | null>(null); const [comment, setComment] = useState("");
  const load = useCallback(() => Promise.all([campaignsApi.get(id), tasksApi.list({ campaign_id: id }), approvalsApi.list({ object_type: "CAMPAIGN_STRATEGY", object_id: id })]).then(([campaignValue, taskRows, approvalRows]) => { setCampaign(campaignValue); setTasks(taskRows); setApprovals(approvalRows); }).catch((reason: Error) => setError(reason.message)), [id]);
  useEffect(() => { if (!authLoading && !user) { router.replace("/login"); return; } if (user) load(); }, [authLoading, user, router, load]);
  useEffect(() => { if (campaign?.status !== "PLANNING") return; const timer = window.setInterval(load, 3000); return () => window.clearInterval(timer); }, [campaign?.status, load]);
  async function archive() { if (!window.confirm("Архивировать кампанию? После архивации редактирование будет недоступно.")) return; try { setCampaign(await campaignsApi.archive(id)); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось архивировать кампанию."); } }
  async function generate() { try { await campaignsApi.generateStrategy(id); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сформировать стратегию."); } }
  async function approve() { try { await campaignsApi.approveStrategy(id); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось согласовать стратегию."); } }
  async function submitDecision() { if (!action || !comment.trim()) { setError("Комментарий обязателен."); return; } try { if (action === "revision") await campaignsApi.requestStrategyRevision(id, comment); else await campaignsApi.rejectStrategy(id, comment); setAction(null); setComment(""); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить решение."); } }
  if (authLoading || (!campaign && !error)) return <main><p>Загружаем кампанию…</p></main>;
  if (!campaign) return <main><section><p role="alert" className="error">{error}</p><Link href="/campaigns">К кампаниям</Link></section></main>;
  const archived = campaign.status === "ARCHIVED";
  return <main><section className="wide"><header className="page-header"><div><p className="eyebrow">Обзор</p><h1>{campaign.name}</h1><p className="status-text">{CAMPAIGN_STATUS_LABELS[campaign.status]}</p></div><Link href="/campaigns">К кампаниям</Link></header>
    {error && <p role="alert" className="error">{error}</p>}<div className="details-grid"><div><dt>Цель</dt><dd>{campaign.goal}</dd></div><div><dt>Продукт</dt><dd>{campaign.product ?? "Не указан"}</dd></div><div><dt>Целевая аудитория</dt><dd>{campaign.target_audience ?? "Не указана"}</dd></div><div><dt>Оффер</dt><dd>{campaign.offer ?? "Не указан"}</dd></div><div><dt>Желаемый результат</dt><dd>{campaign.desired_result ?? "Не указан"}</dd></div><div><dt>Период</dt><dd>{formatDate(campaign.start_date)} — {formatDate(campaign.end_date)}</dd></div><div><dt>Описание</dt><dd>{campaign.description ?? "Нет дополнительного контекста"}</dd></div><div><dt>Кто создал</dt><dd>{campaign.creator.full_name ?? campaign.creator.email}</dd></div><div><dt>Создана</dt><dd>{formatDateTime(campaign.created_at)}</dd></div><div><dt>Изменена</dt><dd>{formatDateTime(campaign.updated_at)}</dd></div></div>
    {!archived && <div className="actions"><Link className="button-link" href={`/campaigns/${id}/edit`}>Редактировать</Link><button className="secondary" onClick={archive}>Архивировать</button></div>}
    <div className="page-header"><h2>Стратегия</h2><span>Версия {campaign.strategy_version}</span></div>
    {campaign.status === "DRAFT" && <div><p>Стратегия ещё не сформирована.</p><button onClick={generate}>Сформировать стратегию</button></div>}
    {campaign.status === "PLANNING" && <div><p>AI-директор формирует стратегию...</p>{tasks.filter((item) => item.task_type === "CAMPAIGN_PLANNING").slice(0, 1).map((item) => <p key={item.id}>Задача: {TASK_STATUS_LABELS[item.status]}</p>)}</div>}
    {campaign.strategy && <div className="strategy"><h3>Резюме</h3><p>{campaign.strategy.campaign_summary}</p><h3>Позиционирование</h3><p>{campaign.strategy.positioning}</p><h3>Целевая аудитория</h3><p>{campaign.strategy.target_audience}</p><h3>Главное сообщение</h3><p>{campaign.strategy.main_message}</p><h3>Контент-стратегия</h3><p>{campaign.strategy.content_strategy}</p><h3>Темы контента</h3><ul>{campaign.strategy.content_topics.map((topic) => <li key={topic}>{topic}</li>)}</ul><h3>Основная статья</h3><p><strong>{campaign.strategy.recommended_article.title}</strong> — {campaign.strategy.recommended_article.objective}</p><h3>SMM-стратегия</h3><p>{campaign.strategy.social_strategy.approach} ({campaign.strategy.social_strategy.post_count} публикаций: {campaign.strategy.social_strategy.channels.join(", ")})</p><h3>План задач</h3><ol>{campaign.strategy.tasks.map((item) => <li key={item.key}><strong>{item.title}</strong> — {item.agent_slug}{item.depends_on.length ? `; зависит от: ${item.depends_on.join(", ")}` : ""}</li>)}</ol></div>}
    {campaign.status === "WAITING_APPROVAL" && <div className="actions"><button onClick={approve}>Утвердить стратегию</button><button className="secondary" onClick={() => setAction("revision")}>Запросить доработку</button><button className="secondary" onClick={() => setAction("reject")}>Отклонить</button></div>}
    {action && <div><label htmlFor="strategy-comment">{action === "revision" ? "Что необходимо изменить?" : "Причина отклонения"}</label><textarea id="strategy-comment" value={comment} onChange={(event) => setComment(event.target.value)} /><div className="actions"><button onClick={submitDecision}>Отправить</button><button className="secondary" onClick={() => setAction(null)}>Отмена</button></div></div>}
    {campaign.status === "ACTIVE" && <p><strong>Стратегия утверждена.</strong> Версия {campaign.strategy_version}.</p>}
    <h3>История согласования</h3>{approvals.length ? <ul>{approvals.map((approval) => <li key={approval.id}>Версия {approval.subject_version}: {approval.status}{approval.resolved_at ? ` — ${formatDateTime(approval.resolved_at)}` : ""}{approval.comment && <p>Комментарий: {approval.comment}</p>}</li>)}</ul> : <p>Согласований пока нет.</p>}
    <div className="page-header"><h2>Задачи</h2>{!archived && <Link className="button-link" href={`/tasks/new?campaign_id=${id}`}>Создать задачу</Link>}</div>
    <div className="summary-grid"><div><strong>{tasks.length}</strong><span>Всего</span></div><div><strong>{tasks.filter((item) => item.status === "READY").length}</strong><span>Ready</span></div><div><strong>{tasks.filter((item) => item.status === "BLOCKED").length}</strong><span>Blocked</span></div><div><strong>{tasks.filter((item) => item.status === "IN_PROGRESS").length}</strong><span>In Progress</span></div><div><strong>{tasks.filter((item) => item.status === "COMPLETED").length}</strong><span>Completed</span></div></div>
    {tasks.length ? <ul>{tasks.map((task) => <li key={task.id}><Link href={`/tasks/${task.id}`}>{task.title}</Link> — {TASK_STATUS_LABELS[task.status]}</li>)}</ul> : <p>У кампании пока нет задач.</p>}
  </section></main>;
}
