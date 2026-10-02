"use client";

import Link from "next/link";
import { Suspense } from "react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { knowledgeApi, knowledgePacksApi, type KnowledgeItem, type KnowledgePack, type KnowledgeSearchResult, type KnowledgeStore } from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";

const STATUS_LABELS = { UPLOADING: "Загружается", INDEXING: "Индексируется", READY: "Готов", FAILED: "Ошибка", ARCHIVED: "Архив" } as const;

function KnowledgePageContent() {
  const { user, loading: authLoading } = useAuth(); const router = useRouter(); const searchParams = useSearchParams(); const campaignId = searchParams.get("campaign_id");
  const [items, setItems] = useState<KnowledgeItem[]>([]); const [loading, setLoading] = useState(true);
  const [error, setError] = useState(""); const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState(""); const [author, setAuthor] = useState("");
  const [query, setQuery] = useState(""); const [results, setResults] = useState<KnowledgeSearchResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [store, setStore] = useState<KnowledgeStore | null>(null);
  const [packs, setPacks] = useState<KnowledgePack[]>([]);
  const loadItems = useCallback(() => knowledgeApi.listItems().then(setItems).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false)), []);
  useEffect(() => { if (!authLoading && !user) { router.replace("/login"); return; } if (user) { void loadItems(); void knowledgeApi.getStore().then(setStore).catch((reason: Error) => setError(reason.message)); if (campaignId) void knowledgePacksApi.list({ campaign_id: campaignId }).then(setPacks).catch((reason: Error) => setError(reason.message)); } }, [authLoading, user, router, loadItems, campaignId]);
  useEffect(() => { if (!items.some((item) => item.status === "UPLOADING" || item.status === "INDEXING")) return; const timer = window.setInterval(() => void loadItems(), 3000); return () => window.clearInterval(timer); }, [items, loadItems]);
  async function upload(event: FormEvent) { event.preventDefault(); if (!file) return; setBusy(true); setError(""); try { await knowledgeApi.upload(file, title, author); setFile(null); setTitle(""); setAuthor(""); await loadItems(); } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); } }
  async function search(event: FormEvent) { event.preventDefault(); setBusy(true); setError(""); try { setResults((await knowledgeApi.search(query)).results); } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); } }
  async function archive(id: string) { if (!window.confirm("Архивировать документ? Он перестанет участвовать в поиске.")) return; await knowledgeApi.archive(id); await loadItems(); }
  async function initialize() { setBusy(true); setError(""); try { setStore(await knowledgeApi.initializeStore()); } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); } }
  if (authLoading || loading) return <main><p>Загружаем базу знаний…</p></main>;
  return <main><section className="wide"><PageBreadcrumbs items={[{ label: "Главная", href: "/" }, { label: "База знаний" }]} /><header className="page-header"><div><p className="eyebrow">Знания iTeam</p><h1>База знаний</h1><p className="muted">Источники, материалы и пакеты знаний, которые доступны кампаниям и агентам.</p></div>{campaignId ? <Link href={`/campaigns/${campaignId}`}>Вернуться к кампании</Link> : <Link href="/campaigns">К кампаниям</Link>}</header>
    {error && <p role="alert" className="error">{error}</p>}
    <section className="knowledge-role card" aria-labelledby="keeper-heading"><p className="eyebrow">AI-роль</p><h2 id="keeper-heading">Хранитель знаний</h2><p>Собирает релевантные материалы из базы знаний и формирует источники для стратегии и статей. Не утверждает материалы и не публикует их.</p><Link href="/agents">Посмотреть роли команды</Link></section>
    <p className="notice">Vector Store: {store ? `${store.name} · ${store.status}` : "не инициализирован"}</p>
    {campaignId && <section className="page-section" aria-labelledby="campaign-packs-heading"><div className="section-heading"><div><p className="eyebrow">Кампания</p><h2 id="campaign-packs-heading">Пакеты исследований</h2></div></div>{packs.length ? <div className="card-stack">{packs.map((pack) => <article className="content-card" key={pack.id}><div className="card-heading"><div><h3>{pack.status === "READY" ? "Пакет готов" : "Не хватает источников"}</h3><p className="muted"><Link href={`/tasks/${pack.task_id}`}>Открыть исследование</Link> · версия стратегии {pack.strategy_version ?? "—"}</p></div><span className="status-badge">{pack.status === "READY" ? "Готов" : "Требует внимания"}</span></div><p>{pack.summary}</p><p className="muted">Источников: {pack.items.length} · {formatDateTime(pack.created_at)}</p>{pack.gaps.length > 0 && <ul>{pack.gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul>}<details><summary>Использованные источники и выдержки</summary><ul>{pack.items.map((item) => <li key={item.result_key}><strong>{item.source_title}</strong>{item.filename && ` · ${item.filename}`}<p>{item.excerpt}</p></li>)}</ul></details></article>)}</div> : <p className="empty-state">Для этой кампании пока нет сохранённых пакетов исследований.</p>}</section>}
    {user?.role === "ADMIN" && !store && <button onClick={() => void initialize()} disabled={busy}>Инициализировать базу знаний</button>}
    {user?.role === "ADMIN" && <form onSubmit={upload}><h2>Загрузить документ</h2><label htmlFor="knowledge-file">Файл PDF, DOCX, TXT или MD</label><input id="knowledge-file" type="file" accept=".pdf,.docx,.txt,.md" onChange={(event) => setFile(event.target.files?.[0] ?? null)} required/><label htmlFor="knowledge-title">Название (необязательно)</label><input id="knowledge-title" value={title} onChange={(event) => setTitle(event.target.value)}/><label htmlFor="knowledge-author">Автор (необязательно)</label><input id="knowledge-author" value={author} onChange={(event) => setAuthor(event.target.value)}/><button disabled={busy}>Загрузить документ</button></form>}
    <h2>Документы</h2>{items.length === 0 ? <div className="empty"><p>Документов пока нет.</p></div> : <div className="table-wrap"><table><thead><tr><th>Название</th><th>Тип</th><th>Статус</th><th>Размер</th><th>Загружен</th><th>Индексирован</th><th></th></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td>{item.title}<br/><small>{item.original_filename}</small>{item.error_message && <p className="error">{item.error_message}</p>}</td><td>{item.content_type.toUpperCase()}</td><td>{STATUS_LABELS[item.status]}</td><td>{item.file_size_bytes == null ? "—" : `${Math.ceil(item.file_size_bytes / 1024)} КБ`}</td><td>{formatDateTime(item.created_at)}</td><td>{formatDateTime(item.indexed_at)}</td><td>{user?.role === "ADMIN" && item.status === "FAILED" && <button className="secondary" onClick={() => void knowledgeApi.retry(item.id).then(loadItems)}>Повторить</button>}{user?.role === "ADMIN" && item.status !== "ARCHIVED" && <button className="secondary" onClick={() => void archive(item.id)}>Архивировать</button>}</td></tr>)}</tbody></table></div>}
    <form onSubmit={search}><h2>Поиск по базе знаний</h2><p className="muted">Поиск запускается только после отправки запроса.</p><label htmlFor="knowledge-query">Запрос</label><input id="knowledge-query" value={query} onChange={(event) => setQuery(event.target.value)} required/><button disabled={busy}>{busy ? "Ищем…" : "Найти"}</button>{busy && <p role="status">Ищем подходящие выдержки…</p>}</form>
    {results.length > 0 && <div className="search-results">{results.map((result) => <article className="agent-card" key={`${result.file_id}-${result.excerpt}`}><h3>{result.source_title}</h3><p><strong>Файл:</strong> {result.filename}</p><p><strong>Релевантность:</strong> {result.score == null ? "не указана" : result.score.toFixed(3)}</p><p>{result.excerpt}</p></article>)}</div>}
  </section></main>;
}

export default function KnowledgePage() {
  return <Suspense fallback={<main><p role="status">Загружаем базу знаний…</p></main>}><KnowledgePageContent /></Suspense>;
}
