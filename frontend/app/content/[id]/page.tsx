"use client";

import Link from "@/components/hash-link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useHashTarget } from "@/components/use-hash-target";
import { PublicationDeliveryNotice } from "@/components/publication-delivery-notice";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { StatusBadge } from "@/components/status-badge";
import { useAuth } from "@/components/auth-provider";
import {
  agentRunsApi,
  campaignsApi,
  contentApi,
  publicationsApi,
  publicationPlansApi,
  tasksApi,
  type AgentRun,
  type Content,
  type ContentRevisionProgress,
  type Publication,
  type PublicationPlan,
  type Task,
} from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";
import { CONTENT_STATUS_LABELS, CONTENT_TYPE_LABELS, PLAN_STATUS_LABELS, PUBLICATION_STATUS_LABELS, RUN_STATUS_LABELS, TASK_TYPE_LABELS, contentStatusHelp } from "@/lib/presentation";

function Markdown({ value }: { value: string }) {
  return <div className="markdown">{value.split(/\n\n+/).map((block, index) => {
    const lines = block.split("\n");
    const first = lines[0];
    if (first.startsWith("# ")) return <h1 key={index}>{first.slice(2)}</h1>;
    if (first.startsWith("## ")) return <h2 key={index}>{first.slice(3)}</h2>;
    if (first.startsWith("### ")) return <h3 key={index}>{first.slice(4)}</h3>;
    return <p key={index}>{lines.join("\n")}</p>;
  })}</div>;
}

function PackView({ value }: { value: Record<string, unknown> }) {
  const posts = Array.isArray(value.posts) ? value.posts.filter((post): post is Record<string, unknown> => typeof post === "object" && post !== null) : [];
  return <div className="content-stack">
    {typeof value.strategy_summary === "string" && <p>{value.strategy_summary}</p>}
    {posts.map((post, index) => (
      <article className="card" key={String(post.key ?? index)}>
        <div className="section-heading"><h3>{String(post.title ?? "Публикация")}</h3><span className="status-badge">{post.channel === "VK" ? "VK" : "Telegram"}</span></div>
        <Markdown value={String(post.text_markdown ?? "")} />
        {post.cta !== undefined && <p><strong>Призыв к действию:</strong> {String(post.cta)}</p>}
        {Array.isArray(post.sources) && post.sources.length > 0 && <p className="subtle">Основано на разделах статьи: {post.sources.map((source) => typeof source === "object" && source !== null && "section_key" in source ? String(source.section_key) : "").filter(Boolean).join(", ")}</p>}
      </article>
    ))}
  </div>;
}

export default function ContentDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [item, setItem] = useState<Content | null>(null);
  const [campaignName, setCampaignName] = useState("");
  const [task, setTask] = useState<Task | null>(null);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [plans, setPlans] = useState<PublicationPlan[]>([]);
  const [publications, setPublications] = useState<Publication[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editMode, setEditMode] = useState(false);
  const [draftText, setDraftText] = useState("");
  const [editComment, setEditComment] = useState("");
  const [revisionMode, setRevisionMode] = useState(false);
  const [revisionComment, setRevisionComment] = useState("");
  const [revisionProgress, setRevisionProgress] = useState<ContentRevisionProgress | null>(null);
  const [rejectMode, setRejectMode] = useState(false);
  const [rejectComment, setRejectComment] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const content = await contentApi.get(params.id);
      setItem(content);
      const [campaign, sourceTask, sourceRuns, campaignPlans, campaignPublications, currentRevision] = await Promise.all([
        campaignsApi.get(content.campaign_id).catch(() => null),
        tasksApi.get(content.source_task_id).catch(() => null),
        agentRunsApi.list({ task_id: content.source_task_id }).catch(() => []),
        publicationPlansApi.list(content.campaign_id).catch(() => []),
        publicationsApi.listCampaign(content.campaign_id).catch(() => []),
        contentApi.revisionProgress(content.id).catch(() => null),
      ]);
      setCampaignName(campaign?.name ?? "Кампания");
      setTask(sourceTask);
      setRuns(sourceRuns);
      setPlans(campaignPlans);
      setPublications(campaignPublications);
      setRevisionProgress(currentRevision);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось загрузить материал.");
    } finally {
      setLoading(false);
    }
  }, [params.id]);

  useEffect(() => {
    if (!authLoading && !user) { router.replace("/login"); return; }
    if (user) void Promise.resolve().then(load);
  }, [authLoading, user, router, load]);

  useEffect(() => {
    if (!revisionProgress || ["COMPLETED", "FAILED", "CANCELLED"].includes(revisionProgress.task_status)) return;
    const timer = window.setInterval(() => {
      void contentApi.revisionProgress(params.id).then(async (next) => {
        setRevisionProgress(next);
        if (next?.task_status === "COMPLETED" || next?.agent_run_status === "COMPLETED") {
          setItem(await contentApi.get(params.id));
        }
      }).catch(() => undefined);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [params.id, revisionProgress]);

  const plan = useMemo(() => plans.find((candidate) => candidate.items.some((planned) => planned.id === item?.publication_plan_item_id)), [plans, item?.publication_plan_item_id]);
  const planItem = plan?.items.find((planned) => planned.id === item?.publication_plan_item_id);
  const exactApproved = item?.approved_version_id ?? null;
  const publication = publications.find((candidate) => candidate.content_item_id === item?.id && candidate.content_version_id === exactApproved && candidate.status !== "CANCELLED")
    ?? publications.find((candidate) => candidate.content_item_id === item?.id && ["DRAFT", "WAITING_APPROVAL", "APPROVED", "SCHEDULED", "PUBLISHING"].includes(candidate.status))
    ?? publications.find((candidate) => candidate.content_item_id === item?.id && candidate.status === "PUBLISHED");
  const structured = item?.current_version?.structured_content ?? {};
  const sourceDerivations = item?.current_version?.derivations ?? [];
  const canEdit = item?.content_type === "ARTICLE" || item?.content_type === "SOCIAL_POST";
  const canRevise = item?.content_type === "ARTICLE" || item?.content_type === "SOCIAL_POST" || (item?.status !== "APPROVED" && item?.content_type === "SOCIAL_POST_PACK");
  const publicationVersion = publication ? item?.versions.find((version) => version.id === publication.content_version_id) : null;
  const hasPublicationVersionConflict = Boolean(publication && item?.current_version && publication.content_version_id !== item.current_version.id);
  const hasScheduledApprovedCurrentVersion = Boolean(item?.content_type === "SOCIAL_POST" && item.status === "APPROVED" && publication?.status === "SCHEDULED" && item.current_version && item.approved_version_id === item.current_version.id && publication.content_version_id === item.approved_version_id);
  const canReplaceScheduledPublication = Boolean(item?.status === "APPROVED" && publication?.status === "SCHEDULED" && item.current_version && publication.content_version_id !== item.current_version.id && item.approved_version_id === item.current_version.id);

  async function approve() { if (!item) return; setBusy(true); try { setItem(await contentApi.approve(item.id)); setNotice("Материал утверждён."); } catch (reason) { setNotice((reason as Error).message); } finally { setBusy(false); } }
  async function reject() { if (!item || !rejectComment.trim()) return; setBusy(true); try { setItem(await contentApi.reject(item.id, rejectComment)); setNotice("Материал отклонён."); setRejectMode(false); setRejectComment(""); } catch (reason) { setNotice((reason as Error).message); } finally { setBusy(false); } }
  async function revise() { if (!item || !revisionComment.trim()) return; setBusy(true); try { setItem(await contentApi.requestRevision(item.id, revisionComment)); const progress = await contentApi.revisionProgress(item.id); setRevisionProgress(progress); setNotice(progress ? "" : "Доработка поставлена в очередь"); setRevisionMode(false); setRevisionComment(""); } catch (reason) { setNotice((reason as Error).message); } finally { setBusy(false); } }
  function beginEdit() { if (!item?.current_version) return; setDraftText(item.current_version.content); setEditComment(""); setEditMode(true); setRevisionMode(false); setRejectMode(false); }
  async function saveEdit() {
    if (!item?.current_version || !draftText.trim()) return;
    setBusy(true);
    try {
      const updated = await contentApi.manualEdit(item.id, { content: draftText, expected_current_version_id: item.current_version.id, change_description: editComment.trim() || null });
      setItem(updated);
      setNotice(`Создана версия v${updated.current_version?.version_number ?? ""}. Она ожидает отдельного утверждения.`);
      setEditMode(false);
    } catch (reason) {
      setNotice((reason as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function schedulePublication() { if (!item) return; setBusy(true); try { const scheduled = await publicationsApi.scheduleContent(item.id); setPublications((rows) => [scheduled, ...rows.filter((row) => row.id !== scheduled.id)]); setNotice("Публикация запланирована по утверждённому плану."); } catch (reason) { setNotice((reason as Error).message); } finally { setBusy(false); } }
  async function replaceScheduledPublication() {
    if (!publication || !item?.current_version || !publicationVersion) return;
    const confirmed = window.confirm(`Заменить запланированную публикацию версии v${publicationVersion.version_number} на утверждённую v${item.current_version.version_number}? Старая публикация будет отменена, новая сохранит канал и время.`);
    if (!confirmed) return;
    setBusy(true);
    try {
      const replacement = await publicationsApi.replaceScheduledVersion(publication.id);
      setPublications((rows) => [replacement, ...rows.map((row) => row.id === publication.id ? { ...row, status: "CANCELLED" as const } : row).filter((row) => row.id !== replacement.id)]);
      setNotice(`Публикация версии v${publicationVersion.version_number} отменена; новая публикация версии v${item.current_version.version_number} запланирована на прежнее время.`);
    } catch (reason) {
      setNotice((reason as Error).message);
    } finally {
      setBusy(false);
    }
  }

  useHashTarget(item);
  if (authLoading || loading) return <main><p role="status">Загружаем материал…</p></main>;
  if (error || !item) return <main className="page"><PageBreadcrumbs items={[{ label: "Контент", href: "/content" }, { label: "Материал" }]} /><div className="empty-state" role="alert"><h1>Не удалось открыть материал</h1><p>{error || "Материал не найден."}</p><button onClick={() => void load()}>Повторить</button></div></main>;

  const revisionIsReady = revisionProgress?.task_status === "COMPLETED" || revisionProgress?.agent_run_status === "COMPLETED";
  const revisionIsRunning = revisionProgress?.task_status === "IN_PROGRESS" || revisionProgress?.agent_run_status === "RUNNING";
  const revisionIsStopped = revisionProgress?.task_status === "FAILED" || revisionProgress?.task_status === "CANCELLED" || revisionProgress?.agent_run_status === "FAILED" || revisionProgress?.agent_run_status === "CANCELLED";
  const revisionIsActive = Boolean(revisionProgress && !revisionIsReady && !revisionIsStopped);
  const revisionIsUnavailable = Boolean(revisionProgress && revisionProgress.task_status === "READY" && revisionProgress.executor_available === false);
  const revisionMessage = revisionIsReady
    ? item.approved_version_id === revisionProgress?.created_content_version_id
      ? "Новая версия доработки утверждена"
      : (item.current_version_id ?? item.current_version?.id) !== revisionProgress?.created_content_version_id
        ? "Доработка завершена; проверьте текущую редакцию"
        : "Новая версия готова и ожидает согласования"
    : revisionIsRunning
      ? "SMM Manager дорабатывает материал…"
      : revisionIsStopped
        ? `Доработка остановлена${revisionProgress?.error_message ? `: ${revisionProgress.error_message}` : "."}`
        : revisionIsUnavailable
          ? "Задача создана, но исполнитель сейчас не запущен"
          : revisionProgress
            ? "Доработка поставлена в очередь"
            : null;

  return (
    <main className="page">
      <PageBreadcrumbs items={[{ label: "Кампании", href: "/campaigns" }, { label: campaignName, href: `/campaigns/${item.campaign_id}` }, { label: "Контент", href: `/content?campaign_id=${item.campaign_id}` }, { label: item.title }]} />
      <header className="page-header">
        <div><p className="eyebrow">{CONTENT_TYPE_LABELS[item.content_type]}</p><h1>{item.title}</h1><div className="row-meta"><StatusBadge status={item.status} label={CONTENT_STATUS_LABELS[item.status]} /><Link href={`/campaigns/${item.campaign_id}`}>{campaignName}</Link>{item.channel && <span>{item.channel === "VK" ? "VK" : "Telegram"}</span>}</div></div>
        <Link className="button-link secondary-link" href={`/campaigns/${item.campaign_id}`}>Открыть кампанию</Link>
      </header>

      {notice && <p className="notice" role="status">{notice}</p>}
      {revisionProgress && revisionMessage && <section className={revisionIsStopped || revisionIsUnavailable ? "warning" : "notice"} aria-live="polite" aria-label="Статус доработки" role="status"><strong>{revisionMessage}</strong><span> · </span><Link href={`/tasks/${revisionProgress.task_id}`}>Открыть задачу</Link>{revisionProgress.agent_run_id && <details><summary>Технические сведения</summary><p>AgentRun: <code>{revisionProgress.agent_run_id}</code></p></details>}</section>}
      <section className="section-block" aria-label="Сведения о материале">
        <div className="section-heading"><h2>Карточка материала</h2><span className="subtle">{contentStatusHelp(item.status)}</span></div>
        <div className="metadata-grid">
          <div className="metadata-item"><strong>Кампания</strong><span><Link href={`/campaigns/${item.campaign_id}`}>{campaignName}</Link></span></div>
          <div className="metadata-item"><strong>Тип</strong><span>{CONTENT_TYPE_LABELS[item.content_type]}</span></div>
          <div className="metadata-item"><strong>Текущая версия</strong><span>{item.current_version ? `v${item.current_version.version_number}` : "Ещё не создана"}</span></div>
          <div className="metadata-item"><strong>Утверждённая версия</strong><span>{item.approved_version_id ? `v${item.versions.find((version) => version.id === item.approved_version_id)?.version_number ?? "зафиксирована"}` : "Нет"}</span></div>
          <div className="metadata-item"><strong>Изменён</strong><span>{formatDateTime(item.updated_at)}</span></div>
          {item.channel && <div className="metadata-item"><strong>Канал</strong><span>{item.plan_channel ?? item.channel}</span></div>}
        </div>
        {(item.status === "WAITING_APPROVAL" || item.status === "APPROVED") && <div id="approval" className="content-stack" style={{ marginTop: "1rem" }}>
          <div className="card"><h3>{item.status === "APPROVED" ? "Новая редакция" : "Редакторское решение"}</h3><p className="subtle">{item.status === "APPROVED" ? "Утверждённая версия останется в истории без изменений. Новая редакция станет отдельной версией и потребует собственного согласования." : <><strong>Утвердить</strong> — разрешить дальнейшее планирование этой версии. <strong>Редактировать</strong> — сохранить ручную правку как новую версию. <strong>Отправить на доработку</strong> — создать задачу профильному AI-исполнителю. <strong>Отклонить</strong> — завершить согласование без публикации.</>}</p>
            {hasScheduledApprovedCurrentVersion && publicationVersion && <aside className="warning" role="note"><strong>Уже запланирована публикация версии v{publicationVersion.version_number}.</strong><p>Если вы создадите новую редакцию, запланированная публикация останется привязана к v{publicationVersion.version_number}. Новая версия не заменит её автоматически.</p></aside>}
            <div className="actions">{item.status === "WAITING_APPROVAL" && <button disabled={busy || revisionIsActive} onClick={() => void approve()}>Утвердить</button>}{canEdit && <button className="secondary" disabled={busy || revisionIsActive} onClick={beginEdit}>{item.status === "APPROVED" ? "Создать новую редакцию" : "Редактировать текст"}</button>}{canRevise && <button className="secondary" disabled={busy || revisionIsActive} onClick={() => { setRevisionMode(true); setEditMode(false); setRejectMode(false); }}>{item.status === "APPROVED" ? "Отправить на доработку" : "Отправить на доработку"}</button>}{item.status === "WAITING_APPROVAL" && <button className="secondary" disabled={busy || revisionIsActive} onClick={() => { setRejectMode(true); setEditMode(false); setRevisionMode(false); }}>Отклонить</button>}</div>
          </div>
          {editMode && <form className="card" onSubmit={(event) => { event.preventDefault(); void saveEdit(); }}><h3>Ручная редактура</h3>{item.content_type === "SOCIAL_POST" && <p className="notice">Меняется только текст. Канал, время, пункт медиаплана и привязка к источникам сохраняются.</p>}<label>Текст материала<textarea aria-label="Текст материала" rows={14} value={draftText} onChange={(event) => setDraftText(event.target.value)} required /></label><label>Комментарий к версии<input aria-label="Комментарий к версии" value={editComment} onChange={(event) => setEditComment(event.target.value)} placeholder="Ручная редактура" /></label><p className="subtle">Сохранение создаст новую версию. Утверждение выполняется отдельным действием.</p><div className="actions"><button disabled={busy || !draftText.trim()} type="submit">Сохранить новую версию</button><button className="secondary" disabled={busy} type="button" onClick={() => setEditMode(false)}>Отмена</button></div></form>}
          {revisionMode && <form className="card" onSubmit={(event) => { event.preventDefault(); void revise(); }}><h3>Отправить на доработку</h3><label>Замечания редактора<textarea aria-label="Замечания редактора" rows={5} value={revisionComment} onChange={(event) => setRevisionComment(event.target.value)} required /></label><p className="subtle">{item.content_type === "SOCIAL_POST" ? "SMM Manager" : "AI-исполнитель"} получит точную текущую версию, замечания и зафиксированные источники. Результат станет новой версией и снова потребует утверждения.</p><div className="actions"><button disabled={busy || !revisionComment.trim()} type="submit">Поставить задачу на доработку</button><button className="secondary" disabled={busy} type="button" onClick={() => setRevisionMode(false)}>Отмена</button></div></form>}
          {rejectMode && <form className="card" onSubmit={(event) => { event.preventDefault(); void reject(); }}><h3>Отклонить материал</h3><label>Причина отклонения<textarea aria-label="Причина отклонения" rows={4} value={rejectComment} onChange={(event) => setRejectComment(event.target.value)} required /></label><div className="actions"><button className="secondary" disabled={busy || !rejectComment.trim()} type="submit">Подтвердить отклонение</button><button className="secondary" disabled={busy} type="button" onClick={() => setRejectMode(false)}>Отмена</button></div></form>}
        </div>}
      </section>

      {item.content_type === "SOCIAL_POST" && item.publication_plan_item_id && <section id="publication" className="section-block" aria-label="План публикации">
        <div className="section-heading"><h2>План публикации</h2>{plan && <StatusBadge status={plan.status} label={PLAN_STATUS_LABELS[plan.status]} />}</div>
        {planItem ? <><div className="metadata-grid"><div className="metadata-item"><strong>Канал</strong><span>{planItem.channel === "VK" ? "VK" : "Telegram"}</span></div><div className="metadata-item"><strong>Дата и время</strong><span>{formatDateTime(planItem.scheduled_at)}</span></div><div className="metadata-item"><strong>Тема</strong><span>{planItem.topic}</span></div><div className="metadata-item"><strong>Исходная статья</strong><span><Link href={`/content/${planItem.source_content_item_id}`}>{item.source_content_item_title ?? sourceDerivations[0]?.source_content_item_title ?? "Открыть источник"}</Link></span></div></div>
          {publication?.status === "SCHEDULED" && <PublicationDeliveryNotice providerEnabled={publication.provider_enabled} />}
          {publication ? <><p className="notice">Публикация: <StatusBadge status={publication.status} label={PUBLICATION_STATUS_LABELS[publication.status]} />{publication.scheduled_at && ` · ${formatDateTime(publication.scheduled_at)} · ${publication.channel === "VK" ? "VK" : "Telegram"}`}</p>{hasPublicationVersionConflict && publication.status === "SCHEDULED" && <p className="warning">Запланирована публикация версии v{publicationVersion?.version_number ?? "?"}. Новая редакция v{item.current_version?.version_number} не заменит её автоматически.</p>}{hasPublicationVersionConflict && publication.status === "PUBLISHED" && <p className="subtle">Опубликованная версия v{publicationVersion?.version_number ?? "?"} сохранена в истории. Заменить опубликованную публикацию нельзя.</p>}{canReplaceScheduledPublication && <button disabled={busy} onClick={() => void replaceScheduledPublication()}>{busy ? "Заменяем…" : "Заменить версию в запланированной публикации"}</button>}</> : <><p>Публикация: ещё не запланирована</p>{item.status === "APPROVED" && exactApproved && plan?.status === "APPROVED" && <button disabled={busy} onClick={() => void schedulePublication()}>{busy ? "Планируем…" : "Запланировать публикацию"}</button>}</>}
        </> : <p className="warning">Плановый пункт не найден. Обратитесь к странице кампании.</p>}
      </section>}

      <section className="content-reading" aria-label="Текст материала">
        <div className="section-heading"><h2>{item.content_type === "SOCIAL_POST_PACK" ? "Пакет публикаций" : "Текст материала"}</h2>{item.current_version && <span className="subtle">Версия {item.current_version.version_number}</span>}</div>
        {!item.current_version ? <div className="empty-state"><p>Текст пока не создан.</p></div> : item.content_type === "SOCIAL_POST_PACK" ? <PackView value={structured} /> : <Markdown value={item.current_version.content} />}
      </section>

      {item.current_version?.created_by_user_id && (sourceDerivations.length > 0 || item.current_version.sources.length > 0) && <p className="notice">Источники и provenance унаследованы от предыдущей версии. Ручные изменения текста не проходили отдельную AI-проверку источников.</p>}

      <section className="section-block" aria-label="Источники и происхождение">
        <div className="section-heading"><h2>Источники и происхождение</h2><span className="subtle">Материал связан с проверяемыми исходными данными</span></div>
        {sourceDerivations.length ? <div className="content-stack">{sourceDerivations.map((source, index) => <article className="source-card" key={`${source.source_content_version_id}-${index}`}><Link className="source-title" href={`/content/${source.source_content_item_id}`}>{source.source_content_item_title}</Link><span className="subtle">Статья · версия {source.source_version_number} · раздел «{source.section_key}»</span><details><summary>Техническая ссылка на версию</summary><code>{source.source_content_version_id}</code></details></article>)}</div> : item.current_version?.sources.length ? <div className="content-stack">{item.current_version.sources.map((source, index) => <article className="source-card" key={`${source.knowledge_pack_item_id}-${index}`}><strong className="source-title">{source.source_title}</strong><span className="subtle">{source.filename ?? "Источник знаний"} · раздел «{source.section_key}»</span><p>{source.excerpt}</p></article>)}</div> : <div className="empty-state"><p>Для этой версии источники не указаны.</p></div>}
        {planItem?.source_claim_ids?.length ? <div className="card"><h3>Использованные тезисы</h3><p>{planItem.source_support_summary ?? "Тезисы зафиксированы в утверждённом плане."}</p><ul>{planItem.source_claim_ids.map((claim) => <li key={claim}>{claim}</li>)}</ul></div> : null}
        <div className="card"><h3>Создание материала</h3>{task ? <><Link href={`/tasks/${task.id}`}>{task.title}</Link><p className="row-meta">Кампания: {campaignName} · Тип задачи: {TASK_TYPE_LABELS[task.task_type]}</p>{runs.length ? runs.slice(0, 3).map((run) => <div className="source-card" key={run.id}><div className="row-meta"><strong>{run.agent?.name ?? "AI-исполнитель"}</strong><span>{RUN_STATUS_LABELS[run.status]}</span><span>{run.model}</span>{run.created_at && <span>{formatDateTime(run.created_at)}</span>}</div>{run.tool_calls?.length ? <p>Инструменты: {run.tool_calls.map((tool) => tool.tool_name ?? tool.name).filter(Boolean).join(", ")}</p> : <p className="subtle">Информация об инструментах не сохранена.</p>}<details><summary>Технические сведения запуска</summary><code>{run.id}</code>{run.trace_id && <p>Trace ID: <code>{run.trace_id}</code></p>}</details></div>) : <p className="subtle">Запуск AI для этой задачи не найден.</p>}</> : <p className="subtle">Связанная задача больше недоступна.</p>}</div>
      </section>

      <section className="section-block" aria-label="История версий">
        <div className="section-heading"><h2>Версии</h2><span className="subtle">Текущая и утверждённая версии отмечены отдельно</span></div>
        {item.versions.length ? <div className="content-stack">{[...item.versions].sort((a, b) => b.version_number - a.version_number).map((version) => <article className="content-row" key={version.id}><div className="content-row-main"><strong>Версия {version.version_number}</strong><div className="row-meta">{version.id === item.current_version?.id && <StatusBadge label="Текущая" status="IN_PROGRESS" />}{version.id === item.approved_version_id && <StatusBadge label="Утверждена" status="APPROVED" />}<span>{version.created_by_user_id ? "Создана редактором" : version.created_by_agent_id ? "Создана AI-исполнителем" : "Автор не указан"}</span><span>{formatDateTime(version.created_at)}</span></div>{version.change_description && <span className="subtle">{version.change_description}</span>}</div>{version.id === item.current_version?.id && <span className="subtle">Текст показан выше</span>}</article>)}</div> : <p>История версий пока пуста.</p>}
      </section>

      <section className="section-block" aria-label="История согласований">
        <div className="section-heading"><h2>Согласования</h2><Link href={`/approvals?object_id=${item.id}`}>Все согласования →</Link></div>
        {item.approval_history?.length ? <div className="timeline">{[...item.approval_history].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()).map((approval) => <article className="timeline-item" key={approval.id}><span>{formatDateTime(approval.resolved_at ?? approval.created_at)}</span><div><strong>Версия {approval.subject_version}: {approval.status === "APPROVED" ? "утверждена" : approval.status === "PENDING" ? "ожидает решения" : approval.status === "REVISION_REQUESTED" ? "запрошена доработка" : "отклонена"}</strong>{approval.comment && <p>{approval.comment}</p>}{approval.reviewed_by_user_id && <small>Проверил пользователь · <code>{approval.reviewed_by_user_id}</code></small>}</div></article>)}</div> : <div className="empty-state"><p>Истории согласований пока нет.</p></div>}
      </section>
    </main>
  );
}
