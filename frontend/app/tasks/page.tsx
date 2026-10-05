"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { StatusBadge } from "@/components/status-badge";
import { agentsApi, campaignsApi, tasksApi, type AgentListItem, type CampaignListItem, type TaskListItem } from "@/lib/api";
import { TASK_PRIORITIES, TASK_PRIORITY_LABELS, TASK_STATUSES, TASK_STATUS_LABELS, TASK_TYPES, TASK_TYPE_LABELS, taskDate } from "@/lib/tasks";

const attention = new Set(["FAILED", "BLOCKED"]);
const active = new Set(["IN_PROGRESS", "WAITING_REVIEW", "WAITING_APPROVAL", "NEW"]);

function TaskRow({ task }: { task: TaskListItem }) {
  return <article className="task-row">
    <div className="task-row-main">
      <Link className="task-row-title" href={`/tasks/${task.id}`}>{task.title}</Link>
      <div className="row-meta"><Link href={`/campaigns/${task.campaign_id}`}>{task.campaign.name}</Link><span>{TASK_TYPE_LABELS[task.task_type]}</span><span>{task.assigned_agent?.name ?? "Исполнитель не назначен"}</span><span>Создана {taskDate(task.created_at)}</span></div>
    </div>
    <div className="row-meta"><StatusBadge status={task.status} label={TASK_STATUS_LABELS[task.status]} />{task.classification_label && <span className="status-badge" data-classification={task.classification}>{task.classification_label}</span>}<span title="Приоритет задачи">{TASK_PRIORITY_LABELS[task.priority]}</span>{task.deadline && <span>Срок: {taskDate(task.deadline)}</span>}</div>
  </article>;
}

export default function TasksPage() {
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [campaigns, setCampaigns] = useState<CampaignListItem[]>([]);
  const [agents, setAgents] = useState<AgentListItem[]>([]);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams(window.location.search);
      const campaignId = params.get("campaign_id");
      const status = params.get("status");
      if (campaignId) setFilters((current) => ({ ...current, campaign_id: campaignId }));
      if (status && TASK_STATUSES.includes(status as (typeof TASK_STATUSES)[number])) setFilters((current) => ({ ...current, status }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    Promise.all([tasksApi.list(), campaignsApi.list(), agentsApi.list()])
      .then(([taskRows, campaignRows, agentRows]) => { setTasks(taskRows); setCampaigns(campaignRows); setAgents(agentRows); })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false));
  }, []);

  const filter = (key: string, value: string) => setFilters((current) => ({ ...current, [key]: value }));
  const visible = useMemo(() => tasks
    .filter((task) => !filters.campaign_id || task.campaign_id === filters.campaign_id)
    .filter((task) => !filters.agent_id || task.assigned_agent?.id === filters.agent_id)
    .filter((task) => !filters.status || task.status === filters.status)
    .filter((task) => !filters.task_type || task.task_type === filters.task_type)
    .filter((task) => !filters.priority || task.priority === filters.priority)
    .sort((a, b) => {
      const rank = (status: string) => status === "FAILED" ? 0 : status === "BLOCKED" ? 1 : status === "READY" ? 2 : status === "IN_PROGRESS" ? 3 : 4;
      return rank(a.status) - rank(b.status) || new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
    }), [tasks, filters]);
  const attentionTasks = visible.filter((task) => task.classification === "actionable" && attention.has(task.status));
  const readyTasks = visible.filter((task) => task.status === "READY");
  const activeTasks = visible.filter((task) => active.has(task.status));
  const historyTasks = visible.filter((task) => task.classification !== "actionable" || ["COMPLETED", "CANCELLED", "APPROVED"].includes(task.status));
  const clearFilters = () => setFilters({});

  return <main className="page">
    <PageBreadcrumbs items={[{ label: "Задачи" }]} />
    <header className="page-header"><div><p className="eyebrow">Рабочий процесс</p><h1>Задачи</h1><p className="page-subtitle">Сначала показываем то, что требует решения, затем готовую и текущую работу. Завершённые задачи доступны в истории.</p></div><Link className="button-link" href="/tasks/new">Новая задача</Link></header>
    <div className="summary-grid" aria-label="Сводка по задачам">
      <a className="metric-card" href="#tasks-attention" title="Ошибки и задачи, ожидающие зависимость"><strong>{tasks.filter((task) => task.classification === "actionable" && attention.has(task.status)).length}</strong><span>Требуют внимания</span></a>
      <a className="metric-card" href="#tasks-ready" title="Можно запустить следующим шагом"><strong>{tasks.filter((task) => task.status === "READY").length}</strong><span>Готовы к выполнению</span></a>
      <a className="metric-card" href="#tasks-active" title="Система или исполнитель уже работает"><strong>{tasks.filter((task) => active.has(task.status)).length}</strong><span>В работе и ожидании</span></a>
      <a className="metric-card" href="#tasks-history" title="Завершённые, отменённые, superseded и исторические задачи"><strong>{tasks.filter((task) => task.classification !== "actionable" || ["COMPLETED", "CANCELLED", "APPROVED"].includes(task.status)).length}</strong><span>В истории</span></a>
    </div>
    <section className="section-block" aria-label="Фильтры задач">
      <div className="filters">
        <div><label htmlFor="task-campaign">Кампания</label><select id="task-campaign" aria-label="Кампания" value={filters.campaign_id ?? ""} onChange={(event) => filter("campaign_id", event.target.value)}><option value="">Все кампании</option>{campaigns.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
        <div><label htmlFor="task-agent">Исполнитель</label><select id="task-agent" aria-label="Агент" value={filters.agent_id ?? ""} onChange={(event) => filter("agent_id", event.target.value)}><option value="">Все исполнители</option>{agents.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
        <div><label htmlFor="task-status">Статус</label><select id="task-status" aria-label="Статус" value={filters.status ?? ""} onChange={(event) => filter("status", event.target.value)}><option value="">Все статусы</option>{TASK_STATUSES.map((item) => <option key={item} value={item}>{TASK_STATUS_LABELS[item]}</option>)}</select></div>
        <div><label htmlFor="task-type">Тип задачи</label><select id="task-type" aria-label="Тип" value={filters.task_type ?? ""} onChange={(event) => filter("task_type", event.target.value)}><option value="">Все типы</option>{TASK_TYPES.map((item) => <option key={item} value={item}>{TASK_TYPE_LABELS[item]}</option>)}</select></div>
        <div><label htmlFor="task-priority">Приоритет</label><select id="task-priority" aria-label="Приоритет" value={filters.priority ?? ""} onChange={(event) => filter("priority", event.target.value)}><option value="">Любой приоритет</option>{TASK_PRIORITIES.map((item) => <option key={item} value={item}>{TASK_PRIORITY_LABELS[item]}</option>)}</select></div>
      </div>
      <button className="secondary" onClick={clearFilters}>Сбросить фильтры</button>
    </section>
    {loading ? <p role="status">Загружаем задачи…</p> : error ? <div className="empty-state" role="alert"><h2>Не удалось загрузить задачи</h2><p>{error}</p><button onClick={() => window.location.reload()}>Повторить</button></div> : visible.length === 0 ? <div className="empty-state"><h2>{tasks.length ? "По этим фильтрам задач нет" : "Задач пока нет"}</h2><p>{tasks.length ? "Измените фильтры или покажите весь список." : "Создайте первую задачу для кампании или сформируйте стратегию."}</p>{tasks.length ? <button className="secondary" onClick={clearFilters}>Сбросить фильтры</button> : <Link className="button-link" href="/campaigns">Перейти к кампаниям</Link>}</div> : <div className="task-groups">
      <section id="tasks-attention" className="section-block"><div className="section-heading"><h2>Требуют внимания <span className="subtle">{attentionTasks.length}</span></h2><p>Ошибки нужно проверить; заблокированные задачи ждут зависимость.</p></div>{attentionTasks.length ? <div className="content-stack">{attentionTasks.map((task) => <TaskRow key={task.id} task={task} />)}</div> : <p className="empty">Ошибок и заблокированных задач нет.</p>}</section>
      <section id="tasks-ready" className="section-block"><div className="section-heading"><h2>Готовы к выполнению <span className="subtle">{readyTasks.length}</span></h2><p>Можно запустить исполнение или открыть задачу.</p></div>{readyTasks.length ? <div className="content-stack">{readyTasks.slice(0, 8).map((task) => <TaskRow key={task.id} task={task} />)}{readyTasks.length > 8 && <details><summary>Показать ещё {readyTasks.length - 8}</summary><div className="content-stack">{readyTasks.slice(8).map((task) => <TaskRow key={task.id} task={task} />)}</div></details>}</div> : <p className="empty">Сейчас нет готовых задач.</p>}</section>
      <section id="tasks-active" className="section-block"><div className="section-heading"><h2>В работе и ожидании <span className="subtle">{activeTasks.length}</span></h2><p>«В работе» означает, что задача выполняется; ожидание обычно связано с проверкой или согласованием.</p></div>{activeTasks.length ? <div className="content-stack">{activeTasks.slice(0, 8).map((task) => <TaskRow key={task.id} task={task} />)}{activeTasks.length > 8 && <details><summary>Показать ещё {activeTasks.length - 8}</summary><div className="content-stack">{activeTasks.slice(8).map((task) => <TaskRow key={task.id} task={task} />)}</div></details>}</div> : <p className="empty">Активных задач нет.</p>}</section>
      <details id="tasks-history" className="disclosure"><summary>Показать завершённые задачи и историю · {historyTasks.length}</summary><div className="disclosure-body">{historyTasks.length ? <div className="content-stack">{historyTasks.map((task) => <TaskRow key={task.id} task={task} />)}</div> : <p>История задач пуста.</p>}</div></details>
    </div>}
  </main>;
}
