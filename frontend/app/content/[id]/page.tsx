"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

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

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const content = await contentApi.get(params.id);
      setItem(content);
      const [campaign, sourceTask, sourceRuns, campaignPlans, campaignPublications] = await Promise.all([
        campaignsApi.get(content.campaign_id).catch(() => null),
        tasksApi.get(content.source_task_id).catch(() => null),
        agentRunsApi.list({ task_id: content.source_task_id }).catch(() => []),
        publicationPlansApi.list(content.campaign_id).catch(() => []),
        publicationsApi.listCampaign(content.campaign_id).catch(() => []),
      ]);
      setCampaignName(campaign?.name ?? "Кампания");
      setTask(sourceTask);
      setRuns(sourceRuns);
      setPlans(campaignPlans);
      setPublications(campaignPublications);
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

  const plan = useMemo(() => plans.find((candidate) => candidate.items.some((planned) => planned.id === item?.publication_plan_item_id)), [plans, item?.publication_plan_item_id]);
  const planItem = plan?.items.find((planned) => planned.id === item?.publication_plan_item_id);
  const exactApproved = item?.approved_version_id ?? null;
  const publication = publications.find((candidate) => candidate.content_item_id === item?.id && candidate.content_version_id === exactApproved && candidate.status !== "CANCELLED")
    ?? publications.find((candidate) => candidate.content_item_id === item?.id && ["DRAFT", "WAITING_APPROVAL", "APPROVED", "SCHEDULED", "PUBLISHING"].includes(candidate.status));
  const structured = item?.current_version?.structured_content ?? {};
  const sourceDerivations = item?.current_version?.derivations ?? [];
  const canRevise = item?.content_type !== "SOCIAL_POST";

  async function approve() { if (!item) return; setBusy(true); try { setItem(await contentApi.approve(item.id)); setNotice("Материал утверждён."); } catch (reason) { setNotice((reason as Error).message); } finally { setBusy(false); } }
  async function reject() { if (!item) return; const comment = window.prompt("Причина отклонения"); if (!comment?.trim()) return; setBusy(true); try { setItem(await contentApi.reject(item.id, comment)); setNotice("Материал отклонён."); } catch (reason) { setNotice((reason as Error).message); } finally { setBusy(false); } }
  async function revise() { if (!item) return; const comment = window.prompt("Что необходимо доработать?"); if (!comment?.trim()) return; setBusy(true); try { setItem(await contentApi.requestRevision(item.id, comment)); setNotice("Запрос на доработку отправлен."); } catch (reason) { setNotice((reason as Error).message); } finally { setBusy(false); } }
  async function schedulePublication() { if (!item) return; setBusy(true); try { const scheduled = await publicationsApi.scheduleContent(item.id); setPublications((rows) => [scheduled, ...rows.filter((row) => row.id !== scheduled.id)]); setNotice("Публикация запланирована по утверждённому плану."); } catch (reason) { setNotice((reason as Error).message); } finally { setBusy(false); } }

  if (authLoading || loading) return <main><p role="status">Загружаем материал…</p></main>;
  if (error || !item) return <main className="page"><PageBreadcrumbs items={[{ label: "Контент", href: "/content" }, { label: "Материал" }]} /><div className="empty-state" role="alert"><h1>Не удалось открыть материал</h1><p>{error || "Материал не найден."}</p><button onClick={() => void load()}>Повторить</button></div></main>;

  return (
    <main className="page">
      <PageBreadcrumbs items={[{ label: "Кампании", href: "/campaigns" }, { label: campaignName, href: `/campaigns/${item.campaign_id}` }, { label: "Контент", href: `/content?campaign_id=${item.campaign_id}` }, { label: item.title }]} />
      <header className="page-header">
        <div><p className="eyebrow">{CONTENT_TYPE_LABELS[item.content_type]}</p><h1>{item.title}</h1><div className="row-meta"><StatusBadge status={item.status} label={CONTENT_STATUS_LABELS[item.status]} /><Link href={`/campaigns/${item.campaign_id}`}>{campaignName}</Link>{item.channel && <span>{item.channel === "VK" ? "VK" : "Telegram"}</span>}</div></div>
        <Link className="button-link secondary-link" href={`/campaigns/${item.campaign_id}`}>Открыть кампанию</Link>
      </header>

      {notice && <p className="notice" role="status">{notice}</p>}
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
        {item.status === "WAITING_APPROVAL" && <div className="actions" style={{ marginTop: "1rem" }}><button disabled={busy} onClick={() => void approve()}>Утвердить материал</button><button className="secondary" disabled={busy} onClick={() => void reject()}>Отклонить</button>{canRevise && <button className="secondary" disabled={busy} onClick={() => void revise()}>Запросить доработку</button>}</div>}
      </section>

      {item.content_type === "SOCIAL_POST" && item.publication_plan_item_id && <section className="section-block" aria-label="План публикации">
        <div className="section-heading"><h2>План публикации</h2>{plan && <StatusBadge status={plan.status} label={PLAN_STATUS_LABELS[plan.status]} />}</div>
        {planItem ? <><div className="metadata-grid"><div className="metadata-item"><strong>Канал</strong><span>{planItem.channel === "VK" ? "VK" : "Telegram"}</span></div><div className="metadata-item"><strong>Дата и время</strong><span>{formatDateTime(planItem.scheduled_at)}</span></div><div className="metadata-item"><strong>Тема</strong><span>{planItem.topic}</span></div><div className="metadata-item"><strong>Исходная статья</strong><span><Link href={`/content/${planItem.source_content_item_id}`}>{item.source_content_item_title ?? sourceDerivations[0]?.source_content_item_title ?? "Открыть источник"}</Link></span></div></div>
          {publication ? <p className="notice">Публикация: <StatusBadge status={publication.status} label={PUBLICATION_STATUS_LABELS[publication.status]} />{publication.scheduled_at && ` · ${formatDateTime(publication.scheduled_at)} · ${publication.channel === "VK" ? "VK" : "Telegram"}`}</p> : <><p>Публикация: ещё не запланирована</p>{item.status === "APPROVED" && exactApproved && plan?.status === "APPROVED" && <button disabled={busy} onClick={() => void schedulePublication()}>{busy ? "Планируем…" : "Запланировать публикацию"}</button>}</>}
        </> : <p className="warning">Плановый пункт не найден. Обратитесь к странице кампании.</p>}
      </section>}

      <section className="content-reading" aria-label="Текст материала">
        <div className="section-heading"><h2>{item.content_type === "SOCIAL_POST_PACK" ? "Пакет публикаций" : "Текст материала"}</h2>{item.current_version && <span className="subtle">Версия {item.current_version.version_number}</span>}</div>
        {!item.current_version ? <div className="empty-state"><p>Текст пока не создан.</p></div> : item.content_type === "SOCIAL_POST_PACK" ? <PackView value={structured} /> : <Markdown value={item.current_version.content} />}
      </section>

      <section className="section-block" aria-label="Источники и происхождение">
        <div className="section-heading"><h2>Источники и происхождение</h2><span className="subtle">Материал связан с проверяемыми исходными данными</span></div>
        {sourceDerivations.length ? <div className="content-stack">{sourceDerivations.map((source, index) => <article className="source-card" key={`${source.source_content_version_id}-${index}`}><Link className="source-title" href={`/content/${source.source_content_item_id}`}>{source.source_content_item_title}</Link><span className="subtle">Статья · версия {source.source_version_number} · раздел «{source.section_key}»</span><details><summary>Техническая ссылка на версию</summary><code>{source.source_content_version_id}</code></details></article>)}</div> : item.current_version?.sources.length ? <div className="content-stack">{item.current_version.sources.map((source, index) => <article className="source-card" key={`${source.knowledge_pack_item_id}-${index}`}><strong className="source-title">{source.source_title}</strong><span className="subtle">{source.filename ?? "Источник знаний"} · раздел «{source.section_key}»</span><p>{source.excerpt}</p></article>)}</div> : <div className="empty-state"><p>Для этой версии источники не указаны.</p></div>}
        {planItem?.source_claim_ids?.length ? <div className="card"><h3>Использованные тезисы</h3><p>{planItem.source_support_summary ?? "Тезисы зафиксированы в утверждённом плане."}</p><ul>{planItem.source_claim_ids.map((claim) => <li key={claim}>{claim}</li>)}</ul></div> : null}
        <div className="card"><h3>Создание материала</h3>{task ? <><Link href={`/tasks/${task.id}`}>{task.title}</Link><p className="row-meta">Кампания: {campaignName} · Тип задачи: {TASK_TYPE_LABELS[task.task_type]}</p>{runs.length ? runs.slice(0, 3).map((run) => <div className="source-card" key={run.id}><div className="row-meta"><strong>{run.agent?.name ?? "AI-исполнитель"}</strong><span>{RUN_STATUS_LABELS[run.status]}</span><span>{run.model}</span>{run.created_at && <span>{formatDateTime(run.created_at)}</span>}</div>{run.tool_calls?.length ? <p>Инструменты: {run.tool_calls.map((tool) => tool.tool_name ?? tool.name).filter(Boolean).join(", ")}</p> : <p className="subtle">Информация об инструментах не сохранена.</p>}<details><summary>Технические сведения запуска</summary><code>{run.id}</code>{run.trace_id && <p>Trace ID: <code>{run.trace_id}</code></p>}</details></div>) : <p className="subtle">Запуск AI для этой задачи не найден.</p>}</> : <p className="subtle">Связанная задача больше недоступна.</p>}</div>
      </section>

      <section className="section-block" aria-label="История версий">
        <div className="section-heading"><h2>Версии</h2><span className="subtle">Текущая и утверждённая версии отмечены отдельно</span></div>
        {item.versions.length ? <div className="content-stack">{[...item.versions].sort((a, b) => b.version_number - a.version_number).map((version) => <article className="content-row" key={version.id}><div className="content-row-main"><strong>Версия {version.version_number}</strong><div className="row-meta">{version.id === item.current_version?.id && <StatusBadge label="Текущая" status="IN_PROGRESS" />}{version.id === item.approved_version_id && <StatusBadge label="Утверждена" status="APPROVED" />}<span>{formatDateTime(version.created_at)}</span></div>{version.change_description && <span className="subtle">{version.change_description}</span>}</div>{version.id === item.current_version?.id && <span className="subtle">Текст показан выше</span>}</article>)}</div> : <p>История версий пока пуста.</p>}
      </section>

      <section className="section-block" aria-label="История согласований">
        <div className="section-heading"><h2>Согласования</h2><Link href={`/approvals?object_id=${item.id}`}>Все согласования →</Link></div>
        {item.approval_history?.length ? <div className="timeline">{[...item.approval_history].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()).map((approval) => <article className="timeline-item" key={approval.id}><span>{formatDateTime(approval.resolved_at ?? approval.created_at)}</span><div><strong>Версия {approval.subject_version}: {approval.status === "APPROVED" ? "утверждена" : approval.status === "PENDING" ? "ожидает решения" : approval.status === "REVISION_REQUESTED" ? "запрошена доработка" : "отклонена"}</strong>{approval.comment && <p>{approval.comment}</p>}{approval.reviewed_by_user_id && <small>Проверил пользователь · <code>{approval.reviewed_by_user_id}</code></small>}</div></article>)}</div> : <div className="empty-state"><p>Истории согласований пока нет.</p></div>}
      </section>
    </main>
  );
}
