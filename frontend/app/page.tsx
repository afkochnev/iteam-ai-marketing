"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { StatusBadge } from "@/components/status-badge";
import {
  approvalsApi,
  campaignsApi,
  systemApi,
  tasksApi,
  type Approval,
  type CampaignListItem,
  type SystemStatus,
  type TaskListItem,
} from "@/lib/api";
import { CAMPAIGN_STATUS_LABELS, formatDate, formatDateTime } from "@/lib/campaigns";
import { TASK_PRIORITY_LABELS, TASK_STATUS_LABELS, TASK_TYPE_LABELS } from "@/lib/tasks";

const attentionStatuses = new Set<TaskListItem["status"]>(["FAILED", "BLOCKED"]);

export default function Home() {
  const { user, loading: authLoading, logout } = useAuth();
  const router = useRouter();
  const [campaigns, setCampaigns] = useState<CampaignListItem[]>([]);
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [errors, setErrors] = useState<string[]>([]);

  const fetchDashboard = useCallback(() => Promise.allSettled([
      campaignsApi.list(),
      tasksApi.list(),
      approvalsApi.list(),
      user?.role === "ADMIN" ? systemApi.status() : Promise.resolve(null),
    ]), [user?.role]);

  const applyDashboard = useCallback((results: Awaited<ReturnType<typeof fetchDashboard>>) => {
    const nextErrors: string[] = [];
    const [campaignResult, taskResult, approvalResult, statusResult] = results;

    if (campaignResult.status === "fulfilled") setCampaigns(campaignResult.value);
    else nextErrors.push("кампании");

    if (taskResult.status === "fulfilled") setTasks(taskResult.value);
    else nextErrors.push("задачи");

    if (approvalResult.status === "fulfilled") setApprovals(approvalResult.value);
    else nextErrors.push("согласования");

    if (statusResult.status === "fulfilled") setStatus(statusResult.value);
    else nextErrors.push("операционный статус");

    setErrors(nextErrors);
    setLoading(false);
  }, []);

  useEffect(() => {
    if (!authLoading && !user) router.replace("/login");
  }, [authLoading, user, router]);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    void fetchDashboard().then((results) => {
      if (!cancelled) applyDashboard(results);
    });
    return () => { cancelled = true; };
  }, [user, fetchDashboard, applyDashboard]);

  const activeCampaigns = useMemo(
    () => campaigns.filter((campaign) => campaign.status === "ACTIVE").slice(0, 3),
    [campaigns],
  );
  const attentionTasks = useMemo(
    () => tasks
      .filter((task) => task.classification === "actionable" && attentionStatuses.has(task.status))
      .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))
      .slice(0, 4),
    [tasks],
  );

  if (authLoading || !user) return <main><p role="status">Проверяем авторизацию…</p></main>;

  const readyTasks = status?.tasks.READY ?? tasks.filter((task) => task.status === "READY").length;
  const inProgressTasks = status?.tasks.IN_PROGRESS ?? tasks.filter((task) => task.status === "IN_PROGRESS").length;
  const pendingApprovals = status?.pending_approvals ?? approvals.filter((approval) => approval.status === "PENDING").length;
  const attentionCount = tasks.filter((task) => task.classification === "actionable" && attentionStatuses.has(task.status)).length;

  async function handleLogout() {
    await logout();
    router.replace("/login");
  }

  return (
    <main className="wide dashboard-main">
      <header className="dashboard-hero">
        <div className="dashboard-hero-copy">
          <p className="eyebrow">Рабочее пространство</p>
          <h1>Маркетинг под контролем</h1>
          <p className="dashboard-intro">Кампании, материалы и задачи команды — в одном месте.</p>
          <p className="dashboard-user">Вы вошли как <strong>{user.full_name ?? user.email}</strong></p>
        </div>
        <button className="secondary dashboard-logout" onClick={handleLogout}>Выйти</button>
      </header>

      {errors.length > 0 && (
        <div className="dashboard-alert" role="alert">
          <div>
            <strong>Не все данные удалось загрузить</strong>
            <p>Проверьте соединение и повторите загрузку: {errors.join(", ")}.</p>
          </div>
          <button className="secondary" onClick={() => { setLoading(true); setErrors([]); void fetchDashboard().then(applyDashboard); }} disabled={loading}>Повторить</button>
        </div>
      )}

      {status && <section aria-label="Операционные предупреждения">
        {(status.overdue_publications ?? 0) > 0 && <p role="alert">Просроченные публикации: {status.overdue_publications}. <Link href="/publications#overdue">Принять решение о расписании</Link></p>}
        {((status.publications?.failed ?? 0) > 0 || (status.publications?.reconciliation_required ?? 0) > 0) && <p role="alert">Есть ошибки отправки или неподтверждённая доставка. <Link href="/publications#attention">Проверить результат; не повторять отправку вслепую</Link></p>}
        {(status.stalled_ready_auto_ai_tasks ?? 0) > 0 && <p role="alert">READY AI-задачи долго не начинают выполнение: {status.stalled_ready_auto_ai_tasks}. <Link href="/tasks?status=READY">Проверить задачи и AI runtime</Link>. Работа workers не подтверждена этим показателем.</p>}
        {(status.publications?.provider_disabled ?? 0) > 0 && <p role="alert">Provider отключён для запланированных публикаций: {status.publications?.provider_disabled}. Согласование не означает доступность доставки. <Link href="/publications">Проверить каналы</Link></p>}
        {status.publishing_providers_enabled && <p>Publishing providers: Telegram {status.publishing_providers_enabled.telegram ? "enabled" : "disabled"}; VK {status.publishing_providers_enabled.vk ? "enabled" : "disabled"}. Runtime heartbeat не подтверждён.</p>}
      </section>}

      <div className="summary-grid dashboard-summary" aria-label="Сводка">
        <Link className="metric-card" href="/campaigns?status=ACTIVE">
          <strong>{loading ? "—" : campaigns.filter((campaign) => campaign.status === "ACTIVE").length}</strong>
          <span>Активные кампании</span>
        </Link>
        <Link className="metric-card" href="/tasks?status=FAILED">
          <strong>{loading ? "—" : attentionCount}</strong>
          <span>Требуют внимания</span>
        </Link>
        <Link className="metric-card" href="/approvals">
          <strong>{loading ? "—" : pendingApprovals}</strong>
          <span>Ожидают согласования</span>
        </Link>
        <Link className="metric-card" href="/tasks?status=READY">
          <strong>{loading ? "—" : readyTasks}</strong>
          <span>Готовы к выполнению</span>
          {inProgressTasks > 0 && <small>В работе: {inProgressTasks}</small>}
        </Link>
      </div>

      <section className="section-block dashboard-shortcut-section" aria-labelledby="dashboard-shortcuts-heading">
        <div className="section-heading">
          <div>
            <h2 id="dashboard-shortcuts-heading">Быстрый переход</h2>
            <p>Откройте нужную часть рабочего пространства.</p>
          </div>
        </div>
        <div className="dashboard-shortcuts">
          <Link href="/campaigns"><span className="shortcut-mark">01</span><strong>Кампании</strong><small>Планы и результаты</small></Link>
          <Link href="/content"><span className="shortcut-mark">02</span><strong>Контент</strong><small>Материалы и версии</small></Link>
          <Link href="/tasks"><span className="shortcut-mark">03</span><strong>Задачи</strong><small>Работа команды</small></Link>
          <Link href="/approvals"><span className="shortcut-mark">04</span><strong>Согласования</strong><small>Решения и проверки</small></Link>
          <Link href="/publications"><span className="shortcut-mark">05</span><strong>Публикации</strong><small>Расписание и история</small></Link>
        </div>
      </section>

      <div className="dashboard-content-grid">
        <section className="section-block dashboard-list-section" aria-labelledby="dashboard-campaigns-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">В работе</p>
              <h2 id="dashboard-campaigns-heading">Активные кампании</h2>
            </div>
            <Link className="inline-link" href="/campaigns">Все кампании</Link>
          </div>
          {loading ? <p role="status">Загружаем кампании…</p> : activeCampaigns.length === 0 ? (
            <p className="empty-state">Сейчас нет активных кампаний.</p>
          ) : (
            <div className="dashboard-list">
              {activeCampaigns.map((campaign) => (
                <Link className="dashboard-campaign-card" href={`/campaigns/${campaign.id}`} key={campaign.id}>
                  <div className="dashboard-card-heading">
                    <strong>{campaign.name}</strong>
                    <StatusBadge status={campaign.status} label={CAMPAIGN_STATUS_LABELS[campaign.status]} />
                  </div>
                  <p>{campaign.goal}</p>
                  <small>{formatDate(campaign.start_date)} — {formatDate(campaign.end_date)}</small>
                </Link>
              ))}
            </div>
          )}
        </section>

        <section className="section-block dashboard-list-section" aria-labelledby="dashboard-tasks-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Следующий шаг</p>
              <h2 id="dashboard-tasks-heading">Требуют внимания</h2>
            </div>
            <Link className="inline-link" href="/tasks">Все задачи</Link>
          </div>
          {loading ? <p role="status">Загружаем задачи…</p> : attentionTasks.length === 0 ? (
            <p className="empty-state">Нет ошибок или заблокированных задач.</p>
          ) : (
            <div className="dashboard-list">
              {attentionTasks.map((task) => (
                <article className="dashboard-task-card" key={task.id}>
                  <div className="dashboard-card-heading">
                    <Link href={`/tasks/${task.id}`}>{task.title}</Link>
                    <StatusBadge status={task.status} label={TASK_STATUS_LABELS[task.status]} tone={task.status === "FAILED" ? "danger" : "neutral"} />
                  </div>
                  <div className="row-meta">
                    <Link href={`/campaigns/${task.campaign_id}`}>{task.campaign.name}</Link>
                    <span>{TASK_TYPE_LABELS[task.task_type]}</span>
                    <span>{TASK_PRIORITY_LABELS[task.priority]}</span>
                  </div>
                  <small>Обновлена {formatDateTime(task.updated_at)}</small>
                </article>
              ))}
            </div>
          )}
        </section>
      </div>

      {status?.last_activity_at && <p className="dashboard-last-activity">Последняя активность: {formatDateTime(status.last_activity_at)}</p>}
    </main>
  );
}
