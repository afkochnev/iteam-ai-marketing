"use client";

import Link from "@/components/hash-link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { PublicationDeliveryNotice } from "@/components/publication-delivery-notice";
import { PublicationScheduleEditor } from "@/components/publication-schedule-editor";
import { StatusBadge } from "@/components/status-badge";
import { useHashTarget } from "@/components/use-hash-target";
import {
  campaignsApi,
  contentApi,
  publicationPlansApi,
  publicationsApi,
  type CampaignListItem,
  type ContentListItem,
  type Publication,
  type PublicationCalendarItem,
  type PublicationPlan,
} from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";
import { PUBLICATION_STATUS_LABELS } from "@/lib/presentation";

type CalendarRow = Omit<PublicationCalendarItem, "provider_enabled"> & {
  provider_enabled?: boolean;
  campaign: CampaignListItem;
  publication_plan_item_id?: string | null;
  retry_allowed?: boolean;
  reconciliation_required?: boolean;
  failure_message?: string | null;
  content_status?: ContentListItem["status"];
  operational_data_complete: boolean;
};

type Candidate = ContentListItem & {
  campaign: CampaignListItem;
  planApproved: boolean;
};

type ReadData = {
  calendar: PublicationCalendarItem[];
  publications: Publication[];
  content: ContentListItem[];
  plans: PublicationPlan[];
};

type ReadKind = keyof ReadData;
type ReadFailure = { campaign: CampaignListItem; kind: ReadKind };
type ActionFeedback = { tone: "success" | "error"; message: string };

const readLabels: Record<ReadKind, string> = {
  calendar: "Календарь",
  publications: "Публикации",
  content: "Материалы",
  plans: "Медиапланы",
};

function LoadingState() {
  return <main><p role="status">Загружаем публикации…</p></main>;
}

export default function PublicationsPage() {
  return <Suspense fallback={<LoadingState />}><PublicationsWorkspace /></Suspense>;
}

function PublicationsWorkspace() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const campaignIdFilter = searchParams.get("campaign_id");
  const { user, loading: authLoading } = useAuth();
  const [rows, setRows] = useState<CalendarRow[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selectedCampaignName, setSelectedCampaignName] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [snapshotTime, setSnapshotTime] = useState<number | null>(null);
  const [feedback, setFeedback] = useState<Record<string, ActionFeedback>>({});
  const [scheduleEditorKey, setScheduleEditorKey] = useState<string | null>(null);
  const cache = useRef(new Map<string, Partial<ReadData>>());
  const [failures, setFailures] = useState<ReadFailure[]>([]);

  useHashTarget(rows);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const loadStartedAt = Date.now();
    const from = new Date(loadStartedAt - 30 * 24 * 60 * 60 * 1000);
    const end = new Date(from.getTime() + 90 * 24 * 60 * 60 * 1000);
    try {
      const allCampaigns = await campaignsApi.list();
      const campaigns = campaignIdFilter
        ? allCampaigns.filter((campaign) => campaign.id === campaignIdFilter)
        : allCampaigns;
      if (campaignIdFilter && campaigns.length === 0) {
        setSelectedCampaignName("");
        setRows([]);
        setCandidates([]);
        setFailures([]);
        setError("Кампания для фильтра публикаций не найдена.");
        return;
      }
      setSelectedCampaignName(campaignIdFilter ? campaigns[0]?.name ?? "" : "");
      const results = await Promise.all(campaigns.map(async (campaign) => {
        const settled = await Promise.allSettled([
          publicationsApi.calendar(campaign.id, from.toISOString(), end.toISOString()),
          publicationsApi.listCampaign(campaign.id),
          contentApi.list({ campaign_id: campaign.id, content_type: "SOCIAL_POST", include_archived: "true" }),
          publicationPlansApi.list(campaign.id),
        ]);
        const kinds: ReadKind[] = ["calendar", "publications", "content", "plans"];
        const previous = cache.current.get(campaign.id) ?? {};
        const data = { ...previous };
        const failed: ReadFailure[] = [];
        settled.forEach((result, index) => {
          const kind = kinds[index];
          if (result.status === "fulfilled") Object.assign(data, { [kind]: result.value });
          else failed.push({ campaign, kind });
        });
        cache.current.set(campaign.id, data);
        const { calendar = [], publications = [], content = [], plans = [] } = data;
        const all = new Map<string, CalendarRow>(calendar.map((item) => [
          item.publication_id,
          { ...item, campaign, operational_data_complete: false },
        ]));
        for (const publication of publications) {
          const contentItem = content.find((item) => item.id === publication.content_item_id);
          if (
            settled[1].status === "rejected"
            && settled[0].status === "fulfilled"
            && all.has(publication.id)
          ) continue;
          all.set(publication.id, {
            publication_id: publication.id,
            content_item_id: publication.content_item_id,
            content_version_id: publication.content_version_id,
            title: contentItem?.title
              ?? all.get(publication.id)?.title
              ?? "Открыть материал",
            content_status: contentItem?.status,
            channel: publication.channel,
            status: publication.status,
            scheduled_at: publication.scheduled_at,
            published_at: publication.published_at,
            external_url: publication.external_url,
            provider_enabled: publication.provider_enabled,
            failure_code: publication.failure_code,
            failure_message: publication.failure_message,
            is_overdue: publication.is_overdue,
            lateness_seconds: publication.lateness_seconds,
            publication_plan_item_id: publication.publication_plan_item_id ?? null,
            retry_allowed: publication.retry_allowed,
            reconciliation_required: publication.reconciliation_required,
            campaign,
            operational_data_complete: true,
          });
        }
        const ready = campaign.status === "ARCHIVED"
          || failed.some((failure) => failure.kind !== "calendar")
          ? []
          : content.filter((item) => (
            item.content_type === "SOCIAL_POST"
            && item.status === "APPROVED"
            && item.approved_version_id
            && item.approved_version_id === item.current_version_id
            && !publications.some((publication) => (
              publication.content_item_id === item.id && publication.status !== "CANCELLED"
            ))
            && !publications.some((publication) => (
              publication.content_item_id === item.id
              && publication.content_version_id === item.approved_version_id
              && publication.status === "CANCELLED"
              && publication.channel === (item.plan_channel ?? item.channel)
            ))
          )).map((item) => ({
            ...item,
            campaign,
            planApproved: plans.some((plan) => (
              plan.status === "APPROVED"
              && plan.items.some((planned) => (
                planned.id === item.publication_plan_item_id && planned.status === "PLANNED"
              ))
            )),
          }));
        return { rows: [...all.values()], candidates: ready, failed };
      }));
      setSnapshotTime(loadStartedAt);
      setFailures(results.flatMap((result) => result.failed));
      setRows(results.flatMap((result) => result.rows));
      setCandidates(results.flatMap((result) => result.candidates));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось загрузить публикации.");
    } finally {
      setLoading(false);
    }
  }, [campaignIdFilter]);

  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/login");
      return;
    }
    if (!user) return;
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [authLoading, user, router, load]);

  function applyPublication(
    publication: Publication,
    context?: { campaign?: CampaignListItem; title?: string; contentStatus?: ContentListItem["status"] },
  ) {
    setRows((current) => {
      const existing = current.find((row) => row.publication_id === publication.id);
      const campaign = context?.campaign ?? existing?.campaign;
      if (!campaign) return current;
      const next: CalendarRow = {
        publication_id: publication.id,
        content_item_id: publication.content_item_id,
        content_version_id: publication.content_version_id,
        title: context?.title ?? existing?.title ?? "Открыть материал",
        content_status: context?.contentStatus ?? existing?.content_status,
        channel: publication.channel,
        status: publication.status,
        scheduled_at: publication.scheduled_at,
        published_at: publication.published_at,
        external_url: publication.external_url,
        provider_enabled: publication.provider_enabled,
        failure_code: publication.failure_code,
        failure_message: publication.failure_message,
        is_overdue: publication.is_overdue,
        lateness_seconds: publication.lateness_seconds,
        publication_plan_item_id: publication.publication_plan_item_id ?? null,
        retry_allowed: publication.retry_allowed,
        reconciliation_required: publication.reconciliation_required,
        campaign,
        operational_data_complete: true,
      };
      return [next, ...current.filter((row) => row.publication_id !== publication.id)];
    });
    setCandidates((current) => current.filter((item) => item.id !== publication.content_item_id));
    const cached = cache.current.get(publication.campaign_id);
    if (cached?.publications) {
      cached.publications = [
        publication,
        ...cached.publications.filter((item) => item.id !== publication.id),
      ];
    }
    if (cached?.calendar) {
      cached.calendar = cached.calendar.map((item) => (
        item.publication_id === publication.id
          ? {
            ...item,
            status: publication.status,
            scheduled_at: publication.scheduled_at,
            published_at: publication.published_at,
            external_url: publication.external_url,
            provider_enabled: publication.provider_enabled ?? item.provider_enabled,
            failure_code: publication.failure_code,
            is_overdue: publication.is_overdue,
            lateness_seconds: publication.lateness_seconds,
          }
          : item
      ));
    }
  }

  async function performAction(
    row: CalendarRow,
    action: () => Promise<Publication>,
    successMessage: string,
  ): Promise<boolean> {
    const key = `publication:${row.publication_id}`;
    setBusyKey(key);
    setActionNotice("");
    setFeedback((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    try {
      const updated = await action();
      applyPublication(updated, { campaign: row.campaign, title: row.title });
      if (updated.id !== row.publication_id && updated.status === "SCHEDULED") {
        setRows((current) => current.map((item) => item.publication_id === row.publication_id ? { ...item, status: "CANCELLED" } : item));
        cache.current.delete(row.campaign.id);
      }
      setActionNotice(successMessage);
      setFeedback((current) => ({
        ...current,
        [key]: { tone: "success", message: successMessage },
      }));
      return true;
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Не удалось выполнить действие.";
      setFeedback((current) => ({
        ...current,
        [key]: { tone: "error", message },
      }));
      return false;
    } finally {
      setBusyKey(null);
    }
  }

  function approvePublication(row: CalendarRow) {
    void performAction(
      row,
      () => publicationsApi.approve(row.publication_id),
      "Публикация согласована.",
    );
  }

  function schedulePublication(row: CalendarRow) {
    setScheduleEditorKey(`publication:${row.publication_id}`);
  }

  async function reuseArchived(row: CalendarRow) {
    if (!window.confirm("Создать копию архивного поста? Она потребует отдельного согласования. Отправки сейчас не будет.")) return;
    setBusyKey(`publication:${row.publication_id}`);
    try { const copy = await contentApi.reuse(row.content_item_id); router.push(`/content/${copy.id}#approval`); }
    catch (reason) { setFeedback((current) => ({ ...current, [`publication:${row.publication_id}`]: { tone: "error", message: reason instanceof Error ? reason.message : "Не удалось создать копию." } })); }
    finally { setBusyKey(null); }
  }

  function cancelPublication(row: CalendarRow) {
    if (!window.confirm("Отменить публикацию? Внешняя отправка выполняться не будет.")) return;
    void performAction(
      row,
      () => publicationsApi.cancel(row.publication_id),
      "Публикация отменена. Внешняя отправка не выполнялась.",
    );
  }

  async function performCandidateAction(
    item: Candidate,
    action: () => Promise<Publication>,
    successMessage: string,
  ): Promise<boolean> {
    const key = `candidate:${item.id}`;
    setBusyKey(key);
    setActionNotice("");
    setFeedback((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    try {
      const publication = await action();
      applyPublication(publication, { campaign: item.campaign, title: item.title, contentStatus: item.status });
      setActionNotice(successMessage);
      setFeedback((current) => ({
        ...current,
        [key]: { tone: "success", message: successMessage },
      }));
      return true;
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Не удалось выполнить действие.";
      setFeedback((current) => ({
        ...current,
        [key]: { tone: "error", message },
      }));
      return false;
    } finally {
      setBusyKey(null);
    }
  }

  function cancelOverdueCandidate(item: Candidate) {
    if (!window.confirm("Отменить просроченный слот? Внешняя отправка выполняться не будет.")) return;
    void performCandidateAction(
      item,
      () => publicationsApi.cancelPlanContent(item.id),
      "Просроченный слот отменён. Внешняя отправка не выполнялась.",
    );
  }

  function rescheduleOverdueCandidate(item: Candidate) {
    setScheduleEditorKey(`candidate:${item.id}`);
  }

  function publishNow(row: CalendarRow) {
    if (!window.confirm("Поставить точную согласованную версию в очередь для отправки сейчас?")) return;
    void performAction(
      row,
      () => publicationsApi.publishNow(row.publication_id),
      "Публикация поставлена в очередь на отправку.",
    );
  }

  function retryPublication(row: CalendarRow) {
    void performAction(
      row,
      () => publicationsApi.retry(row.publication_id),
      "Повторная отправка поставлена в очередь.",
    );
  }

  function reconcilePublished(row: CalendarRow) {
    const externalId = window.prompt("Внешний ID публикации");
    if (!externalId?.trim()) return;
    const externalUrl = window.prompt("Внешняя ссылка (необязательно)") ?? "";
    const publishedAt = window.prompt("Время внешней публикации ISO (необязательно)") ?? "";
    const note = window.prompt("Комментарий (необязательно)") ?? "";
    void performAction(
      row,
      () => publicationsApi.reconcilePublished(row.publication_id, {
        external_id: externalId.trim(),
        external_url: externalUrl || undefined,
        published_at: publishedAt || undefined,
        note: note || undefined,
      }),
      "Внешняя публикация подтверждена.",
    );
  }

  function reconcileNotPublished(row: CalendarRow) {
    if (!window.confirm("Подтвердить, что внешней публикации нет?")) return;
    const note = window.prompt("Комментарий (необязательно)") ?? "";
    void performAction(
      row,
      () => publicationsApi.reconcileNotPublished(row.publication_id, note || undefined),
      "Отсутствие внешней публикации подтверждено.",
    );
  }

  const upcoming = rows.filter((item) => (
    item.status === "SCHEDULED" && item.scheduled_at && !item.is_overdue
  )).sort((a, b) => Date.parse(a.scheduled_at!) - Date.parse(b.scheduled_at!));
  const overdue = rows.filter((item) => item.status === "SCHEDULED" && item.is_overdue);
  const active = rows.filter((item) => (
    ["DRAFT", "WAITING_APPROVAL", "APPROVED", "PUBLISHING"].includes(item.status)
  ));
  const published = rows.filter((item) => item.status === "PUBLISHED");
  const cancelledHistory = rows.filter((item) => item.status === "CANCELLED");
  const hasReplacement = (item: CalendarRow) => rows.some((other) => {
    const copy = cache.current.get(other.campaign.id)?.content?.find((content) => content.id === other.content_item_id);
    return (other.content_item_id === item.content_item_id || copy?.reuse_source_content_id === item.content_item_id) && other.publication_id !== item.publication_id && ["SCHEDULED", "PUBLISHING", "PUBLISHED"].includes(other.status);
  });
  const cancelled = cancelledHistory.filter((item) => !hasReplacement(item));
  const attention = rows.filter((item) => (
    item.status !== "CANCELLED" && (item.status === "FAILED" || item.failure_code)
  ));
  const overdueCandidates = candidates.filter((item) => (
    item.planApproved
    && item.plan_scheduled_at
    && snapshotTime !== null
    && Date.parse(item.plan_scheduled_at) <= snapshotTime
  ));
  const overdueCandidateIds = new Set(overdueCandidates.map((item) => item.id));
  const ready = candidates.filter((item) => item.planApproved && !overdueCandidateIds.has(item.id));
  const needsPlan = candidates.filter((item) => !item.planApproved);
  const publicationSourceArchived = (item: CalendarRow) => (
    item.content_status === "ARCHIVED" || item.campaign.status === "ARCHIVED"
  );
  const readyIncomplete = failures.some((failure) => failure.kind !== "calendar");
  const rowsIncomplete = failures.some((failure) => (
    failure.kind === "publications" || failure.kind === "calendar"
  ));
  const incomplete = (
    <p className="notice">Данные загружены не полностью. Отсутствие записей пока не подтверждено.</p>
  );
  const version = (item: CalendarRow) => (
    <span>Версия публикации: <code>{item.content_version_id}</code></span>
  );
  const delivery = (item: CalendarRow) => (
    <PublicationDeliveryNotice providerEnabled={item.provider_enabled} />
  );
  const candidateFeedback = (item: Candidate) => {
    const value = feedback[`candidate:${item.id}`];
    if (!value) return null;
    return (
      <p role={value.tone === "error" ? "alert" : "status"} className={value.tone === "error" ? "error" : "notice"}>
        {value.message}
      </p>
    );
  };
  const actionFeedback = (item: CalendarRow) => {
    const value = feedback[`publication:${item.publication_id}`];
    if (!value) return null;
    return (
      <p role={value.tone === "error" ? "alert" : "status"} className={value.tone === "error" ? "error" : "notice"}>
        {value.message}
      </p>
    );
  };
  const actionUnavailable = (item: CalendarRow) => !item.operational_data_complete && (
    <p className="notice">Операционные действия временно недоступны: сведения о публикации загружены не полностью.</p>
  );
  const publicationScheduleEditor = (item: CalendarRow) => {
    const key = `publication:${item.publication_id}`;
    if (scheduleEditorKey !== key) return null;
    const rescheduling = item.status === "SCHEDULED" || item.status === "CANCELLED";
    return <PublicationScheduleEditor
      editorKey={key}
      title={rescheduling ? "Перенести публикацию" : "Назначить дату публикации"}
      submitLabel={rescheduling ? "Сохранить перенос" : "Запланировать"}
      initialScheduledAt={item.scheduled_at}
      busy={busyKey === key}
      onClose={() => setScheduleEditorKey(null)}
      onSubmit={(scheduledAt) => performAction(
        item,
        () => item.status === "CANCELLED" || item.publication_plan_item_id !== null ? publicationsApi.replan(item.publication_id, scheduledAt) : publicationsApi.schedule(item.publication_id, scheduledAt),
        rescheduling ? "Новое время публикации сохранено." : "Публикация запланирована.",
      )}
    />;
  };
  const candidateScheduleEditor = (item: Candidate) => {
    const key = `candidate:${item.id}`;
    if (scheduleEditorKey !== key) return null;
    return <PublicationScheduleEditor
      editorKey={key}
      title="Перенести публикацию"
      submitLabel="Сохранить перенос"
      busy={busyKey === key}
      onClose={() => setScheduleEditorKey(null)}
      onSubmit={(scheduledAt) => performCandidateAction(
        item,
        () => publicationsApi.reschedulePlanContent(item.id, scheduledAt),
        "Новое время сохранено отдельным решением оператора.",
      )}
    />;
  };

  if (authLoading) return <LoadingState />;

  return <main className="page">
    <PageBreadcrumbs items={[{ label: "Публикации" }]} />
    <header className="page-header">
      <div>
        <p className="eyebrow">Расписание и факт доставки</p>
        <h1>Публикации</h1>
        <p className="page-subtitle">Единое рабочее место для планирования, решений оператора, отправки и истории публикаций. Время показано в часовом поясе браузера.</p>
      </div>
      <Link className="button-link secondary" href="/campaigns">К кампаниям</Link>
    </header>
    {campaignIdFilter && selectedCampaignName && <p className="notice">
      Показана кампания «{selectedCampaignName}». <Link href="/publications">Показать все кампании</Link>
    </p>}
    <p className="notice">Планирование не означает отправку. Доставка требует отдельного разрешения человека и доступного publishing runtime.</p>
    {actionNotice && <p className="notice" role="status">{actionNotice}</p>}
    <div className="summary-grid metric-cards">
      <a className="metric-card" href="#ready"><strong>{readyIncomplete ? "—" : ready.length}</strong><span>Готовы к планированию</span></a>
      <a className="metric-card" href="#active"><strong>{rowsIncomplete ? "—" : active.length}</strong><span>Требуют действия / В процессе</span></a>
      <a className="metric-card" href="#upcoming"><strong>{rowsIncomplete ? "—" : upcoming.length}</strong><span>Предстоящие</span></a>
      <a className="metric-card" href="#overdue"><strong>{rowsIncomplete || readyIncomplete ? "—" : overdue.length + overdueCandidates.length}</strong><span>Просрочено</span></a>
      <a className="metric-card" href="#published"><strong>{rowsIncomplete ? "—" : published.length}</strong><span>Опубликовано</span></a>
      <a className="metric-card" href="#cancelled"><strong>{rowsIncomplete ? "—" : cancelled.length}</strong><span>Отменено</span></a>
      <a className="metric-card" href="#attention"><strong>{rowsIncomplete ? "—" : attention.length}</strong><span>Требуют проверки</span></a>
    </div>
    {loading && <p role="status">Загружаем календарь…</p>}
    {error ? <div className="empty-state" role="alert">
      <h2>Не удалось загрузить публикации</h2>
      <p>{error}</p>
      <button onClick={() => void load()}>Повторить</button> <Link href="/content">Открыть контент</Link>
    </div> : <>
      {failures.map((failure) => <div className="notice" role="alert" key={`${failure.campaign.id}-${failure.kind}`}>
        <strong>{readLabels[failure.kind]} · {failure.campaign.name}</strong>
        <p>Не удалось обновить этот раздел. Успешно загруженные данные сохранены; список может быть неполным.</p>
        <button disabled={loading} onClick={() => void load()}>Повторить загрузку: {readLabels[failure.kind]}</button>
      </div>)}

      <section className="section-block" id="ready" aria-label="Готовы к планированию">
        <h2>Готовы к планированию</h2>
        {ready.length ? <div className="content-stack">{ready.map((item) => <article className="content-card" key={item.id}>
          <h3><Link href={`/content/${item.id}`}>{item.title}</Link></h3>
          <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.plan_channel ?? item.channel} · утверждена версия v{item.approved_version_number ?? item.current_version_number}{item.plan_scheduled_at ? ` · ${formatDateTime(item.plan_scheduled_at)}` : ""}</p>
          <Link className="button-link" href={`/content/${item.id}#publication`}>Открыть пост и запланировать</Link>
        </article>)}</div> : readyIncomplete || loading ? incomplete : <p className="empty-state">Готовых к планированию постов пока нет. <Link href="/content">Проверить контент и согласования</Link></p>}
        {needsPlan.length > 0 && <div>
          <h3>Утверждённые посты без утверждённого пункта плана</h3>
          {needsPlan.map((item) => <p key={item.id}><Link href={`/content/${item.id}`}>{item.title}</Link> · <Link href={`/campaigns/${item.campaign.id}/plan`}>Открыть медиаплан</Link></p>)}
        </div>}
      </section>

      <section className="section-block" id="overdue" aria-labelledby="overdue-heading">
        <h2 id="overdue-heading">Просрочены / Требуют решения</h2>
        {overdue.length || overdueCandidates.length ? <div className="content-stack">
          {overdueCandidates.map((item) => <article className="content-card" id={`plan-content-${item.id}`} tabIndex={-1} key={`plan-content-${item.id}`}>
            <h3><Link href={`/content/${item.id}`}>{item.title}</Link></h3>
            <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.plan_channel ?? item.channel} · Плановое время: {item.plan_scheduled_at ? formatDateTime(item.plan_scheduled_at) : "—"}</p>
            <p>Утверждённая версия v{item.approved_version_number ?? item.current_version_number}</p>
            <p role="alert">Плановый слот прошёл до создания Publication. Требуется отдельное решение оператора.</p>
            <div className="actions">
              <button disabled={busyKey === `candidate:${item.id}`} onClick={() => rescheduleOverdueCandidate(item)}>Перенести</button>
              <button className="secondary" disabled={busyKey === `candidate:${item.id}`} onClick={() => cancelOverdueCandidate(item)}>Отменить слот</button>
            </div>
            {candidateScheduleEditor(item)}
            {candidateFeedback(item)}
            <p className="subtle">Утверждённый медиаплан останется в истории с исходной датой; решение оператора будет записано отдельно.</p>
          </article>)}
          {overdue.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
          <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
          <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.channel} · Плановое время: {item.scheduled_at ? formatDateTime(item.scheduled_at) : "—"}</p>
          {version(item)}
          <p>OVERDUE · Просрочка: {Math.ceil((item.lateness_seconds ?? 0) / 60)} мин.</p>
          <p role="alert">Плановое время прошло; автоматическая отправка остановлена до решения.</p>
          {delivery(item)}
          {item.operational_data_complete ? <div className="actions">
            {item.provider_enabled !== false && <button disabled={busyKey === `publication:${item.publication_id}`} onClick={() => publishNow(item)}>Опубликовать сейчас</button>}
            <button className="secondary" disabled={busyKey === `publication:${item.publication_id}`} onClick={() => cancelPublication(item)}>Отменить</button>
          </div> : actionUnavailable(item)}
          {actionFeedback(item)}
          <p>Перенос публикации из утверждённого медиаплана требует отдельного изменения медиаплана.</p>
          <Link href={`/campaigns/${item.campaign.id}/plan`}>Проверить медиаплан и расписание</Link>
        </article>)}
        </div> : rowsIncomplete || readyIncomplete || loading ? incomplete : <p>Просроченных публикаций нет.</p>}
      </section>

      <section className="section-block" id="active" aria-labelledby="active-heading">
        <h2 id="active-heading">Требуют действия / В процессе</h2>
        {active.length ? <div className="content-stack">{active.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
          <div className="card-heading"><div>
            <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
            <p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel}{item.scheduled_at ? ` · ${formatDateTime(item.scheduled_at)}` : ""}</span>{version(item)}</p>
          </div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>
          {delivery(item)}
          {publicationSourceArchived(item) && <p className="warning" role="alert">Материал или кампания находятся в архиве. Согласование и планирование недоступны; эту публикацию можно только отменить.</p>}
          {item.status === "PUBLISHING" ? <p role="status">Отправка выполняется / результат ещё не подтверждён. Повторная отправка недоступна.</p> : item.operational_data_complete ? <div className="actions">
            {!publicationSourceArchived(item) && ["DRAFT", "WAITING_APPROVAL"].includes(item.status) && <button disabled={busyKey === `publication:${item.publication_id}`} onClick={() => approvePublication(item)}>Согласовать публикацию</button>}
            {!publicationSourceArchived(item) && item.status === "APPROVED" && item.publication_plan_item_id === null && <button disabled={busyKey === `publication:${item.publication_id}`} onClick={() => schedulePublication(item)}>Назначить время</button>}
            <button className="secondary" disabled={busyKey === `publication:${item.publication_id}`} onClick={() => cancelPublication(item)}>{publicationSourceArchived(item) ? "Отменить публикацию" : "Отменить"}</button>
          </div> : actionUnavailable(item)}
          {publicationScheduleEditor(item)}
          {actionFeedback(item)}
        </article>)}</div> : rowsIncomplete || loading ? incomplete : <p className="empty-state">Публикаций, требующих действия или находящихся в процессе отправки, нет.</p>}
      </section>

      <section className="section-block" id="upcoming" aria-label="Предстоящие">
        <h2>Предстоящие</h2>
        {upcoming.length ? <div className="content-stack">{upcoming.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
          <div className="card-heading"><div>
            <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
            <p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel === "VK" ? "VK" : "Telegram"} · {formatDateTime(item.scheduled_at!)}</span>{version(item)}</p>
          </div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>
          {delivery(item)}
          {item.operational_data_complete ? <div className="actions">
            <button disabled={busyKey === `publication:${item.publication_id}`} onClick={() => schedulePublication(item)}>Перенести</button>
            <button className="secondary" disabled={busyKey === `publication:${item.publication_id}`} onClick={() => cancelPublication(item)}>Отменить</button>
          </div> : actionUnavailable(item)}
          {publicationScheduleEditor(item)}
          {item.publication_plan_item_id && <p className="muted">Перенос создаёт новую запись с аудитом решения. Утверждённый медиаплан сохраняется в истории без изменений.</p>}
          {actionFeedback(item)}
          <Link href={`/content/${item.content_item_id}#publication`}>Проверить пост и запланированную версию</Link>
        </article>)}</div> : rowsIncomplete || loading ? incomplete : <p className="empty-state">Запланированных публикаций пока нет. <a href="#ready">Проверить готовые посты</a></p>}
      </section>

      <section className="section-block" id="attention" aria-label="Ошибки отправки">
        <h2>Ошибки отправки</h2>
        {attention.length ? <div className="content-stack">{attention.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
          <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
          <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.failure_code ?? "Статус требует проверки"}</p>
          {item.failure_message && <p>{item.failure_message}</p>}
          {version(item)}
          <StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} />
          {delivery(item)}
          {item.operational_data_complete ? <div className="actions">
            {item.reconciliation_required && <>
              <button disabled={busyKey === `publication:${item.publication_id}`} onClick={() => reconcilePublished(item)}>Подтвердить публикацию</button>
              <button className="secondary" disabled={busyKey === `publication:${item.publication_id}`} onClick={() => reconcileNotPublished(item)}>Подтвердить отсутствие публикации</button>
            </>}
            {item.retry_allowed && <button disabled={busyKey === `publication:${item.publication_id}`} onClick={() => retryPublication(item)}>Повторить</button>}
          </div> : actionUnavailable(item)}
          {actionFeedback(item)}
        </article>)}</div> : rowsIncomplete || loading ? incomplete : <p className="empty-state">Ошибок отправки нет.</p>}
      </section>

      <section className="section-block" id="cancelled" aria-label="Отменённые">
        <h2>Отменённые</h2>
        {cancelled.length ? <div className="content-stack">{cancelled.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
          <div className="card-heading"><div>
            <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
            <p className="row-meta"><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link><span>{item.channel === "VK" ? "VK" : "Telegram"}{item.scheduled_at ? ` · ${formatDateTime(item.scheduled_at)}` : ""}</span>{version(item)}</p>
          </div><StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} /></div>
          <p>Внешняя отправка не выполняется. Запись сохранена в истории публикаций.</p>
          {!publicationSourceArchived(item) && <div className="actions"><Link href={`/content/${item.content_item_id}#edit`}>Редактировать материал</Link><button disabled={busyKey === `publication:${item.publication_id}`} onClick={() => schedulePublication(item)}>Запланировать заново</button></div>}
          {publicationScheduleEditor(item)}{actionFeedback(item)}
          {item.content_status === "ARCHIVED" && <><p className="subtle">Архивный оригинал сохраняется. Копия потребует отдельного согласования и нового выбора времени.</p><button disabled={busyKey === `publication:${item.publication_id}`} onClick={() => void reuseArchived(item)}>Создать копию для новой публикации</button></>}
        </article>)}</div> : rowsIncomplete || loading ? incomplete : <p className="empty-state">Отменённых публикаций нет.</p>}
      </section>

      <details className="disclosure" id="cancelled-history"><summary>История отменённых публикаций · {cancelledHistory.length}</summary><div className="disclosure-body">{cancelledHistory.map((item) => <p key={item.publication_id}><Link href={`/content/${item.content_item_id}`}>{item.title}</Link> · {item.scheduled_at ? formatDateTime(item.scheduled_at) : "Без даты"}{hasReplacement(item) ? " · Есть новая запланированная или опубликованная запись" : " · Отменена"}</p>)}</div></details>
      <details className="disclosure" id="published">
        <summary>Опубликованные · {rowsIncomplete ? "неполные данные" : published.length}</summary>
        <div className="disclosure-body">{published.length ? published.map((item) => <article className="content-card" id={`publication-${item.publication_id}`} tabIndex={-1} key={item.publication_id}>
          <h3><Link href={`/content/${item.content_item_id}`}>{item.title}</Link></h3>
          <p><Link href={`/campaigns/${item.campaign.id}`}>{item.campaign.name}</Link> · {item.channel}{item.published_at ? ` · ${formatDateTime(item.published_at)}` : ""}</p>
          {version(item)}
          <StatusBadge status={item.status} label={PUBLICATION_STATUS_LABELS[item.status]} />
          {item.external_url && <a href={item.external_url} target="_blank" rel="noreferrer">Открыть публикацию</a>}
        </article>) : rowsIncomplete || loading ? incomplete : <p>История публикаций пока пуста.</p>}</div>
      </details>
    </>}
  </main>;
}
