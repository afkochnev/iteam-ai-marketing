"use client";

import Link from "@/components/hash-link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { PublicationChannelChange } from "@/components/publication-channel-change";
import { PublicationDeliveryNotice } from "@/components/publication-delivery-notice";
import { PublicationReplanningPreview } from "@/components/publication-replanning-preview";
import { PublicationScheduleEditor } from "@/components/publication-schedule-editor";
import { StatusBadge } from "@/components/status-badge";
import { useHashTarget } from "@/components/use-hash-target";
import {
  contentApi,
  distributionWorkspaceApi,
  publicationsApi,
  type DistributionCandidate,
  type DistributionPublication,
  type DistributionPublicationAction,
  type DistributionSection,
  type DistributionWorkspace,
} from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";
import { PUBLICATION_STATUS_LABELS } from "@/lib/presentation";

type ActionFeedback = { tone: "success" | "error"; message: string };
type PagePosition = { cursor: string | null; previous: Array<string | null> };
type PageSnapshot = { data: DistributionWorkspace; position: PagePosition };
const firstPage = (): PagePosition => ({ cursor: null, previous: [] });
const PAGE_LIMIT = 50;

function LoadingState() {
  return <main><p role="status">Загружаем публикации…</p></main>;
}

export default function PublicationsPage() {
  return <Suspense fallback={<LoadingState />}><PublicationsScope /></Suspense>;
}

function PublicationsScope() {
  const campaignId = useSearchParams().get("campaign_id");
  // A scope change starts with no snapshot or pending forms from the old campaign.
  return <PublicationsWorkspace key={campaignId ?? "all"} campaignIdFilter={campaignId} />;
}

function PublicationsWorkspace({ campaignIdFilter }: { campaignIdFilter: string | null }) {
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [snapshot, setSnapshot] = useState<PageSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Record<string, ActionFeedback>>({});
  const [scheduleEditorKey, setScheduleEditorKey] = useState<string | null>(null);
  const currentPosition = useRef<PagePosition>(firstPage());
  const requestedPosition = useRef<PagePosition>(firstPage());
  const requestSequence = useRef(0);
  const mounted = useRef(false);

  const load = useCallback(async (position: PagePosition = currentPosition.current): Promise<void> => {
    if (!mounted.current) return;
    const sequence = ++requestSequence.current;
    requestedPosition.current = position;
    setLoading(true);
    setStale(true);
    setError("");
    setScheduleEditorKey(null);
    try {
      const data = await distributionWorkspaceApi.get({
        campaign_id: campaignIdFilter ?? undefined,
        limit: PAGE_LIMIT,
        cursor: position.cursor ?? undefined,
      });
      if (!mounted.current || sequence !== requestSequence.current) return;
      currentPosition.current = position;
      setSnapshot({ data, position });
      setStale(false);
    } catch (reason) {
      if (!mounted.current || sequence !== requestSequence.current) return;
      setError(reason instanceof Error ? reason.message : "Не удалось загрузить публикации.");
      setStale(true);
    } finally {
      if (mounted.current && sequence === requestSequence.current) setLoading(false);
    }
  }, [campaignIdFilter]);

  useEffect(() => {
    mounted.current = true;
    if (!authLoading && !user) {
      router.replace("/login");
      return () => { mounted.current = false; requestSequence.current += 1; };
    }
    const timer = user ? window.setTimeout(() => void load(firstPage()), 0) : undefined;
    return () => {
      mounted.current = false;
      requestSequence.current += 1;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [authLoading, user, router, load]);

  const workspace = snapshot?.data;
  const rows = (workspace?.items ?? []).filter((item): item is DistributionPublication => item.kind === "publication");
  const candidates = (workspace?.items ?? []).filter((item): item is DistributionCandidate => item.kind === "candidate");
  useHashTarget(rows);

  // Membership and available commands are supplied by the server, including history.
  const section = (name: DistributionSection) => rows.filter((row) => row.sections.includes(name));
  const upcoming = section("upcoming").sort((left, right) => (
    (left.scheduled_at ? Date.parse(left.scheduled_at) : Infinity)
    - (right.scheduled_at ? Date.parse(right.scheduled_at) : Infinity)
    || left.publication_id.localeCompare(right.publication_id)
  ));
  const overdue = section("overdue");
  const active = section("active");
  const published = section("published");
  const cancelled = section("cancelled");
  const cancelledHistory = section("cancelled_history");
  const attention = section("attention");
  const ready = candidates.filter((item) => item.state === "ready");
  const needsPlan = candidates.filter((item) => item.state === "needs_plan");
  const overdueCandidates = candidates.filter((item) => item.state === "overdue");
  const actionsUnavailable = loading || stale || !workspace || busyKey !== null;
  const can = (item: DistributionPublication, action: DistributionPublicationAction) => item.allowed_actions.includes(action);
  const count = (name: DistributionSection) => workspace && !stale && !loading ? workspace.counts[name] : "—";
  const selectedCampaignName = workspace?.campaign?.name ?? "";
  const empty = (message: string) => !workspace || stale || loading
    ? <p className="notice">Данные ещё не подтверждены. Обновите снимок публикаций.</p>
    : <p className="empty-state">{message}</p>;

  async function performCommand(
    key: string,
    action: () => Promise<unknown>,
    successMessage: string,
  ): Promise<boolean> {
    if (actionsUnavailable) return false;
    setBusyKey(key);
    setActionNotice("");
    setFeedback((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    try {
      await action();
      if (!mounted.current) return true;
      setActionNotice(successMessage);
      setFeedback((current) => ({ ...current, [key]: { tone: "success", message: successMessage } }));
      // A command response is not a workspace snapshot. Reload every related fact.
      await load();
      return true;
    } catch (reason) {
      if (mounted.current) {
        const message = reason instanceof Error ? reason.message : "Не удалось выполнить действие.";
        setFeedback((current) => ({ ...current, [key]: { tone: "error", message } }));
      }
      return false;
    } finally {
      if (mounted.current) setBusyKey(null);
    }
  }

  function approvePublication(item: DistributionPublication) {
    void performCommand(`publication:${item.publication_id}`, () => publicationsApi.approve(item.publication_id), "Публикация согласована.");
  }

  function cancelPublication(item: DistributionPublication) {
    if (!window.confirm("Отменить публикацию? Внешняя отправка выполняться не будет.")) return;
    void performCommand(`publication:${item.publication_id}`, () => publicationsApi.cancel(item.publication_id), "Публикация отменена. Внешняя отправка не выполнялась.");
  }

  function publishNow(item: DistributionPublication) {
    if (!window.confirm("Поставить точную согласованную версию в очередь для отправки сейчас?")) return;
    void performCommand(`publication:${item.publication_id}`, () => publicationsApi.publishNow(item.publication_id), "Публикация поставлена в очередь на отправку.");
  }

  function retryPublication(item: DistributionPublication) {
    void performCommand(`publication:${item.publication_id}`, () => publicationsApi.retry(item.publication_id), "Повторная отправка поставлена в очередь.");
  }

  function reconcilePublished(item: DistributionPublication) {
    const externalId = window.prompt("Внешний ID публикации");
    if (!externalId?.trim()) return;
    const externalUrl = window.prompt("Внешняя ссылка (необязательно)") ?? "";
    const publishedAt = window.prompt("Время внешней публикации ISO (необязательно)") ?? "";
    const note = window.prompt("Комментарий (необязательно)") ?? "";
    void performCommand(`publication:${item.publication_id}`, () => publicationsApi.reconcilePublished(item.publication_id, {
      external_id: externalId.trim(),
      external_url: externalUrl || undefined,
      published_at: publishedAt || undefined,
      note: note || undefined,
    }), "Внешняя публикация подтверждена.");
  }

  function reconcileNotPublished(item: DistributionPublication) {
    if (!window.confirm("Подтвердить, что внешней публикации нет?")) return;
    const note = window.prompt("Комментарий (необязательно)") ?? "";
    void performCommand(`publication:${item.publication_id}`, () => publicationsApi.reconcileNotPublished(item.publication_id, note || undefined), "Отсутствие внешней публикации подтверждено.");
  }

  function cancelOverdueCandidate(item: DistributionCandidate) {
    if (!window.confirm("Отменить просроченный слот? Внешняя отправка выполняться не будет.")) return;
    void performCommand(`candidate:${item.id}`, () => publicationsApi.cancelPlanContent(item.id), "Просроченный слот отменён. Внешняя отправка не выполнялась.");
  }

  async function reuseArchived(item: DistributionPublication) {
    if (!window.confirm("Создать копию архивного поста? Она потребует отдельного согласования. Отправки сейчас не будет.")) return;
    let copyId: string | null = null;
    const success = await performCommand(`publication:${item.publication_id}`, async () => {
      const copy = await contentApi.reuse(item.content_item_id);
      copyId = copy.id;
    }, "Копия создана. Её нужно согласовать отдельно.");
    if (success && copyId && mounted.current) router.push(`/content/${copyId}#approval`);
  }

  const actionFeedback = (key: string) => {
    const value = feedback[key];
    return value && <p role={value.tone === "error" ? "alert" : "status"} className={value.tone === "error" ? "error" : "notice"}>{value.message}</p>;
  };
  const version = (item: DistributionPublication) => <span>Версия публикации: <code>{item.content_version_id}</code></span>;
  const candidateVersion = (item: DistributionCandidate) => `v${item.approved_version_number}`;
  const delivery = (item: DistributionPublication) => <PublicationDeliveryNotice providerEnabled={stale ? undefined : item.provider_enabled} />;
  const scheduleEditor = (item: DistributionPublication) => {
    const key = `publication:${item.publication_id}`;
    if (scheduleEditorKey !== key || (!can(item, "schedule") && !can(item, "replan"))) return null;
    const replan = !can(item, "schedule") && can(item, "replan");
    const rescheduling = replan || item.sections.some((name) => ["upcoming", "overdue", "cancelled"].includes(name));
    return <PublicationScheduleEditor
      editorKey={key}
      title={rescheduling ? "Перенести публикацию" : "Назначить дату публикации"}
      submitLabel={rescheduling ? "Сохранить перенос" : "Запланировать"}
      initialScheduledAt={item.scheduled_at}
      busy={actionsUnavailable}
      onClose={() => setScheduleEditorKey(null)}
      onSubmit={(scheduledAt) => performCommand(
        key,
        () => replan ? publicationsApi.replan(item.publication_id, scheduledAt) : publicationsApi.schedule(item.publication_id, scheduledAt),
        rescheduling ? "Новое время публикации сохранено." : "Публикация запланирована.",
      )}
    />;
  };
  const candidateScheduleEditor = (item: DistributionCandidate) => {
    const key = `candidate:${item.id}`;
    if (scheduleEditorKey !== key || !item.allowed_actions.includes("reschedule_plan")) return null;
    return <PublicationScheduleEditor
      editorKey={key}
      title="Перенести публикацию"
      submitLabel="Сохранить перенос"
      busy={actionsUnavailable}
      onClose={() => setScheduleEditorKey(null)}
      onSubmit={(scheduledAt) => performCommand(key, () => publicationsApi.reschedulePlanContent(item.id, scheduledAt), "Новое время сохранено отдельным решением оператора.")}
    />;
  };
  const scheduleButton = (item: DistributionPublication, label: string) => (can(item, "schedule") || can(item, "replan"))
    && <button disabled={actionsUnavailable} onClick={() => setScheduleEditorKey(`publication:${item.publication_id}`)}>{label}</button>;
  const cancelButton = (item: DistributionPublication) => can(item, "cancel")
    && <button className="secondary" disabled={actionsUnavailable} onClick={() => cancelPublication(item)}>{item.content_source_archived ? "Отменить публикацию" : "Отменить"}</button>;
  const publishNowButton = (item: DistributionPublication) => can(item, "publish_now")
    && <button disabled={actionsUnavailable} onClick={() => publishNow(item)}>Опубликовать сейчас</button>;

  if (authLoading) return <LoadingState />;

  return <main className="page">
    <PageBreadcrumbs items={[{ label: campaignIdFilter ? selectedCampaignName || "Кампания" : "Главная", href: campaignIdFilter ? `/campaigns/${campaignIdFilter}` : "/" }, { label: "Публикации" }]} />
    <header className="page-header">
      <div>
        <p className="eyebrow">Расписание и факт доставки</p>
        <h1>Публикации</h1>
        <p className="page-subtitle">Рабочее место для планирования, решений оператора, отправки и истории публикаций. Время показано в часовом поясе браузера.</p>
      </div>
      <Link className="button-link secondary" href="/campaigns">К кампаниям</Link>
    </header>
    {campaignIdFilter && selectedCampaignName && <p className="notice">Показана кампания «{selectedCampaignName}». <Link href="/publications">Показать все кампании</Link></p>}
    <p className="notice">Планирование не означает отправку. Доставка требует отдельного разрешения человека и доступного сервиса отправки.</p>
    <div className="actions">
      <button className="secondary" disabled={loading || busyKey !== null} onClick={() => void load()}>Обновить публикации</button>
      {workspace && <p>Снимок на {formatDateTime(workspace.as_of)}{stale ? " · свежесть не подтверждена" : ""}</p>}
    </div>
    <p className="subtle">Показатели и списки относятся только к текущей странице. Каждая страница загружается отдельным снимком.</p>
    <div className="summary-grid metric-cards" aria-label="Показатели текущей страницы">
      <a className="metric-card" href="#ready"><strong>{count("ready")}</strong><span>Готовы к планированию</span></a>
      <a className="metric-card" href="#active"><strong>{count("active")}</strong><span>Требуют действия / В процессе</span></a>
      <a className="metric-card" href="#upcoming"><strong>{count("upcoming")}</strong><span>Предстоящие</span></a>
      <a className="metric-card" href="#overdue"><strong>{count("overdue")}</strong><span>Просрочено</span></a>
      <a className="metric-card" href="#published"><strong>{count("published")}</strong><span>Опубликовано</span></a>
      <a className="metric-card" href="#cancelled"><strong>{count("cancelled")}</strong><span>Отменено</span></a>
      <a className="metric-card" href="#attention"><strong>{count("attention")}</strong><span>Требуют проверки</span></a>
    </div>
    {loading && <p role="status">Загружаем публикации…</p>}
    {error && <div className="empty-state" role="alert">
      <h2>Не удалось загрузить публикации</h2>
      <p>{error}</p>
      {workspace && <p>Показан предыдущий снимок. Данные могут быть устаревшими; действия недоступны до успешного обновления.</p>}
      <button disabled={loading} onClick={() => void load(requestedPosition.current)}>Повторить</button> <Link href="/content">Открыть контент</Link>
    </div>}
    {actionNotice && <p className="notice" role="status">{actionNotice}</p>}
    {!actionsUnavailable && <>
      <PublicationChannelChange onApplied={load} rows={rows.filter((row) => can(row, "change_channel")).map((row) => ({ ...row, operational_data_complete: true }))} />
      <PublicationReplanningPreview onApplied={load} rows={rows.filter((row) => can(row, "batch_replan")).map((row) => ({ ...row, operational_data_complete: true }))} />
    </>}
    <section className="section-block" id="ready" aria-label="Готовы к планированию">
      <h2>Готовы к планированию</h2>
      {ready.length ? <div className="content-stack">{ready.map((item) => <article className="content-card" key={item.id}>
        <h3><Link href={`/content/${item.id}`}>{item.title}</Link></h3>
        <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.plan_channel ?? item.channel} · утверждена версия {candidateVersion(item)}{item.plan_scheduled_at ? ` · ${formatDateTime(item.plan_scheduled_at)}` : ""}</p>
        <Link className="button-link" href={`/content/${item.id}#publication`}>Открыть пост и запланировать</Link>
      </article>)}</div> : empty("На этой странице нет готовых к планированию постов.")}
      {needsPlan.length > 0 && <div>
        <h3>Утверждённые посты без утверждённого пункта плана</h3>
        {needsPlan.map((item) => <p key={item.id}><Link href={`/content/${item.id}`}>{item.title}</Link> · <Link href={`/campaigns/${item.campaign.id}/plan`}>Открыть медиаплан</Link></p>)}
      </div>}
    </section>
    <section className="section-block" id="overdue" aria-labelledby="overdue-heading">
      <h2 id="overdue-heading">Просрочены / Требуют решения</h2>
      {overdue.length || overdueCandidates.length ? <div className="content-stack">
        {overdueCandidates.map((item) => <article className="content-card" id={`plan-content-${item.id}`} tabIndex={-1} key={item.id}>
          <h3><Link href={`/content/${item.id}`}>{item.title}</Link></h3>
          <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.plan_channel ?? item.channel} · Плановое время: {item.plan_scheduled_at ? formatDateTime(item.plan_scheduled_at) : "—"}</p>
          <p>Утверждённая версия {candidateVersion(item)}</p>
          <p role="alert">Плановый слот прошёл до создания Publication. Требуется отдельное решение оператора.</p>
          <div className="actions">
            {item.allowed_actions.includes("reschedule_plan") && <button disabled={actionsUnavailable} onClick={() => setScheduleEditorKey(`candidate:${item.id}`)}>Перенести</button>}
            {item.allowed_actions.includes("cancel_plan") && <button className="secondary" disabled={actionsUnavailable} onClick={() => cancelOverdueCandidate(item)}>Отменить слот</button>}
          </div>
          {candidateScheduleEditor(item)}{actionFeedback(`candidate:${item.id}`)}
          <p className="subtle">Утверждённый медиаплан останется в истории с исходной датой; решение оператора будет записано отдельно.</p>
        </article>)}
        {overdue.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
          <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
          <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.channel} · Плановое время: {item.scheduled_at ? formatDateTime(item.scheduled_at) : "—"}</p>
          {version(item)}
          <p>OVERDUE · Просрочка: {Math.ceil(item.lateness_seconds / 60)} мин.</p>
          <p role="alert">Плановое время прошло; автоматическая отправка остановлена до решения.</p>
          {delivery(item)}
          <div className="actions">
            {publishNowButton(item)}
            {scheduleButton(item, "Перенести")}{cancelButton(item)}
          </div>
          {scheduleEditor(item)}{actionFeedback(`publication:${item.publication_id}`)}
          <Link href={`/campaigns/${item.campaign.id}/plan`}>Проверить медиаплан и расписание</Link>
        </article>)}
      </div> : empty("На этой странице нет просроченных публикаций.")}
    </section>
    <section className="section-block" id="active" aria-labelledby="active-heading">
      <h2 id="active-heading">Требуют действия / В процессе</h2>
      {active.length ? <div className="content-stack">{active.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
        <div className="card-heading"><div>
          <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
          <p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel}{item.scheduled_at ? ` · ${formatDateTime(item.scheduled_at)}` : ""}</span>{version(item)}</p>
        </div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>
        {delivery(item)}
        {item.content_source_archived && <p className="warning" role="alert">Материал или кампания находятся в архиве. Согласование и планирование недоступны; эту публикацию можно только отменить.</p>}
        {item.status === "PUBLISHING" && <p role="status">Отправка выполняется / результат ещё не подтверждён. Повторная отправка недоступна.</p>}
        <div className="actions">
          {can(item, "approve") && <button disabled={actionsUnavailable} onClick={() => approvePublication(item)}>Согласовать публикацию</button>}
          {scheduleButton(item, "Назначить время")}{publishNowButton(item)}{cancelButton(item)}
        </div>
        {scheduleEditor(item)}{actionFeedback(`publication:${item.publication_id}`)}
      </article>)}</div> : empty("На этой странице нет публикаций, требующих действия или находящихся в процессе отправки.")}
    </section>
    <section className="section-block" id="upcoming" aria-label="Предстоящие">
      <h2>Предстоящие</h2>
      {upcoming.length ? <div className="content-stack">{upcoming.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
        <div className="card-heading"><div>
          <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
          <p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel === "VK" ? "VK" : "Telegram"} · {item.scheduled_at ? formatDateTime(item.scheduled_at) : "—"}</span>{version(item)}</p>
        </div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>
        {delivery(item)}
        <div className="actions">{scheduleButton(item, "Перенести")}{publishNowButton(item)}{cancelButton(item)}</div>
        {scheduleEditor(item)}
        {can(item, "replan") && <p className="muted">Перенос создаёт новую запись с аудитом решения. Утверждённый медиаплан сохраняется в истории без изменений.</p>}
        {actionFeedback(`publication:${item.publication_id}`)}
        <Link href={`/content/${item.content_item_id}#publication`}>Проверить пост и запланированную версию</Link>
      </article>)}</div> : empty("На этой странице нет предстоящих публикаций.")}
    </section>
    <section className="section-block" id="attention" aria-label="Ошибки отправки">
      <h2>Ошибки отправки</h2>
      {attention.length ? <div className="content-stack">{attention.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
        <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
        <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.failure_code ?? "Статус требует проверки"}</p>
        {item.failure_message && <p>{item.failure_message}</p>}
        {version(item)}<StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} />{delivery(item)}
        <div className="actions">
          {can(item, "reconcile_published") && <button disabled={actionsUnavailable} onClick={() => reconcilePublished(item)}>Подтвердить публикацию</button>}
          {can(item, "reconcile_not_published") && <button className="secondary" disabled={actionsUnavailable} onClick={() => reconcileNotPublished(item)}>Подтвердить отсутствие публикации</button>}
          {can(item, "retry") && <button disabled={actionsUnavailable} onClick={() => retryPublication(item)}>Повторить</button>}
        </div>
        {actionFeedback(`publication:${item.publication_id}`)}
      </article>)}</div> : empty("На этой странице нет ошибок отправки.")}
    </section>
    <section className="section-block" id="cancelled" aria-label="Отменённые">
      <h2>Отменённые</h2>
      {cancelled.length ? <div className="content-stack">{cancelled.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
        <div className="card-heading"><div>
          <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
          <p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel}{item.scheduled_at ? ` · ${formatDateTime(item.scheduled_at)}` : ""}</span>{version(item)}</p>
        </div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>
        <p>Внешняя отправка не выполняется. Запись сохранена в истории публикаций.</p>
        <a href="#cancelled-history">История отменённых публикаций на этой странице</a>
        {(can(item, "schedule") || can(item, "replan")) && <div className="actions"><Link href={`/content/${item.content_item_id}#edit`}>Редактировать материал</Link>{scheduleButton(item, "Запланировать заново")}</div>}
        {scheduleEditor(item)}{actionFeedback(`publication:${item.publication_id}`)}
        {can(item, "reuse") && <><p className="subtle">Архивный оригинал сохраняется. Копия потребует отдельного согласования и нового выбора времени.</p><button disabled={actionsUnavailable} onClick={() => void reuseArchived(item)}>Создать копию для новой публикации</button></>}
      </article>)}</div> : empty("На этой странице нет отменённых публикаций, требующих решения.")}
    </section>
    <details className="disclosure" id="cancelled-history">
      <summary>История отменённых публикаций на странице · {count("cancelled_history")}</summary>
      <div className="disclosure-body">{cancelledHistory.map((item) => <p key={item.publication_id}><Link href={`/content/${item.content_item_id}`}>{item.title}</Link> · {item.scheduled_at ? formatDateTime(item.scheduled_at) : "Без даты"}{item.has_replacement ? " · Есть новая запланированная или опубликованная запись" : " · Отменена"}</p>)}</div>
    </details>
    <details className="disclosure" id="published">
      <summary>Опубликованные на странице · {count("published")}</summary>
      <div className="disclosure-body">{published.length ? published.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
        <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
        <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.channel}{item.published_at ? ` · ${formatDateTime(item.published_at)}` : ""}</p>
        {version(item)}<StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} />
        {item.external_url && <a href={item.external_url} target="_blank" rel="noreferrer">Открыть публикацию</a>}
      </article>) : empty("На этой странице нет опубликованных записей.")}</div>
    </details>
    {snapshot && <nav className="actions" aria-label="Страницы публикаций">
      <button className="secondary" disabled={loading || stale || busyKey !== null || !snapshot.position.previous.length} onClick={() => {
        const previous = snapshot.position.previous;
        void load({ cursor: previous[previous.length - 1], previous: previous.slice(0, -1) });
      }}>Предыдущая страница</button>
      <span>Страница {snapshot.position.previous.length + 1}</span>
      <button className="secondary" disabled={loading || stale || busyKey !== null || !workspace?.pagination.has_more || !workspace.pagination.next_cursor} onClick={() => {
        if (!workspace?.pagination.next_cursor) return;
        void load({ cursor: workspace.pagination.next_cursor, previous: [...snapshot.position.previous, snapshot.position.cursor] });
      }}>Следующая страница</button>
    </nav>}
  </main>;
}
