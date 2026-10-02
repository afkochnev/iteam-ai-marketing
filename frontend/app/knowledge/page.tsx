"use client";

import Link from "next/link";
import { Suspense } from "react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import {
  knowledgeApi,
  knowledgePacksApi,
  type KnowledgeItem,
  type KnowledgePack,
  type KnowledgeSearchResult,
  type KnowledgeSource,
  type KnowledgeStore,
} from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";

const STATUS_LABELS = {
  UPLOADING: "Загружается",
  INDEXING: "Индексируется",
  READY: "Готов",
  FAILED: "Ошибка",
  ARCHIVED: "Архив",
} as const;
const SOURCE_STATUS_LABELS = { ACTIVE: "Активен", ERROR: "Ошибка", INACTIVE: "Неактивен" } as const;
const SOURCE_TYPE_LABELS = {
  FILE_UPLOAD: "Загрузка файла",
  WEBSITE: "Сайт",
  GOOGLE_DRIVE: "Google Drive",
  YOUTUBE: "YouTube",
} as const;

function KnowledgePageContent() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const campaignId = searchParams.get("campaign_id");
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [packs, setPacks] = useState<KnowledgePack[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<KnowledgeSearchResult[]>([]);
  const [activeAction, setActiveAction] = useState<"upload" | "search" | "initialize" | null>(null);
  const [itemBusyId, setItemBusyId] = useState<string | null>(null);
  const [store, setStore] = useState<KnowledgeStore | null>(null);

  const loadItems = useCallback(
    () =>
      knowledgeApi
        .listItems()
        .then(setItems)
        .catch((reason: Error) => setError(reason.message))
        .finally(() => setLoading(false)),
    [],
  );
  const loadSources = useCallback(
    () => knowledgeApi.listSources().then(setSources).catch((reason: Error) => setError(reason.message)),
    [],
  );
  const loadPacks = useCallback(
    () =>
      knowledgePacksApi
        .list(campaignId ? { campaign_id: campaignId } : {})
        .then(setPacks)
        .catch((reason: Error) => setError(reason.message)),
    [campaignId],
  );

  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/login");
      return;
    }
    if (!user) return;
    void loadItems();
    void loadSources();
    void loadPacks();
    void knowledgeApi
      .getStore()
      .then(setStore)
      .catch((reason: Error) => setError(reason.message));
  }, [authLoading, user, router, loadItems, loadSources, loadPacks]);

  useEffect(() => {
    if (!items.some((item) => item.status === "UPLOADING" || item.status === "INDEXING")) return;
    const timer = window.setInterval(() => void loadItems(), 3000);
    return () => window.clearInterval(timer);
  }, [items, loadItems]);

  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setActiveAction("upload");
    setError("");
    setNotice("");
    try {
      await knowledgeApi.upload(file, title, author);
      setFile(null);
      setTitle("");
      setAuthor("");
      await Promise.all([loadItems(), loadSources()]);
      setNotice("Документ загружен и поставлен в очередь индексирования.");
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setActiveAction(null);
    }
  }

  async function search(event: FormEvent) {
    event.preventDefault();
    setActiveAction("search");
    setError("");
    setNotice("");
    try {
      setResults((await knowledgeApi.search(query)).results);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setActiveAction(null);
    }
  }

  async function archive(item: KnowledgeItem) {
    if (!window.confirm("Архивировать документ? Он перестанет участвовать в поиске.")) return;
    setItemBusyId(item.id);
    setError("");
    setNotice("");
    try {
      await knowledgeApi.archive(item.id);
      await loadItems();
      setNotice("Документ архивирован и больше не участвует в поиске.");
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setItemBusyId(null);
    }
  }

  async function retry(item: KnowledgeItem) {
    setItemBusyId(item.id);
    setError("");
    setNotice("");
    try {
      await knowledgeApi.retry(item.id);
      await loadItems();
      setNotice("Документ повторно поставлен в очередь индексирования.");
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setItemBusyId(null);
    }
  }

  async function initialize() {
    setActiveAction("initialize");
    setError("");
    setNotice("");
    try {
      setStore(await knowledgeApi.initializeStore());
      setNotice("База знаний готова к использованию.");
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setActiveAction(null);
    }
  }

  if (authLoading || loading) {
    return <main><p role="status">Загружаем базу знаний…</p></main>;
  }

  return (
    <main>
      <section className="wide">
        <PageBreadcrumbs items={[{ label: "Главная", href: "/" }, { label: "База знаний" }]} />
        <header className="page-header">
          <div>
            <p className="eyebrow">Знания iTeam</p>
            <h1>База знаний</h1>
            <p className="muted">Источники, документы и пакеты знаний, которые используют кампании и агенты.</p>
          </div>
          {campaignId ? <Link href={`/campaigns/${campaignId}`}>Вернуться к кампании</Link> : <Link href="/campaigns">К кампаниям</Link>}
        </header>

        {error && <p role="alert" className="error">{error}</p>}
        {notice && <p role="status" className="notice">{notice}</p>}

        <section className="knowledge-role card" aria-labelledby="keeper-heading">
          <p className="eyebrow">AI-роль</p>
          <h2 id="keeper-heading">Хранитель знаний</h2>
          <p>Принимает источники, помогает индексировать и структурировать знания, передаёт выдержки другим ролям и сохраняет provenance. Не утверждает материалы и не публикует их.</p>
          <Link href="/agents">Посмотреть роли команды</Link>
        </section>

        <p className="notice">Vector Store: {store ? `${store.name} · ${store.status}` : "не инициализирован"}</p>

        <section className="page-section" aria-labelledby="knowledge-sources-heading">
          <div className="section-heading">
            <div><p className="eyebrow">Источники</p><h2 id="knowledge-sources-heading">Источники знаний</h2></div>
          </div>
          {sources.length ? (
            <ul className="simple-list">
              {sources.map((source) => (
                <li key={source.id}>
                  <span>
                    <strong>{source.name}</strong>
                    <small>{SOURCE_TYPE_LABELS[source.source_type]} · документов: {items.filter((item) => item.source_id === source.id).length}</small>
                  </span>
                  <span>{source.source_url ? <a href={source.source_url} target="_blank" rel="noreferrer">Открыть источник</a> : "Загруженные файлы"} · {SOURCE_STATUS_LABELS[source.status]}</span>
                </li>
              ))}
            </ul>
          ) : <p className="empty-state">Источники появятся после загрузки документов.</p>}
        </section>

        <section className="page-section" aria-labelledby="knowledge-packs-heading">
          <div className="section-heading">
            <div><p className="eyebrow">Исследования</p><h2 id="knowledge-packs-heading">Пакеты знаний</h2></div>
          </div>
          {packs.length ? (
            <div className="card-stack">
              {packs.map((pack) => (
                <article className="content-card" key={pack.id}>
                  <div className="card-heading">
                    <div>
                      <h3>{pack.status === "READY" ? "Пакет готов" : "Не хватает источников"}</h3>
                      <p className="muted">
                        {!campaignId && <><Link href={`/campaigns/${pack.campaign_id}`}>Открыть кампанию</Link> · </>}
                        <Link href={`/tasks/${pack.task_id}`}>Открыть исследование</Link> · версия стратегии {pack.strategy_version ?? "—"}
                      </p>
                    </div>
                    <span className="status-badge">{pack.status === "READY" ? "Готов" : "Требует внимания"}</span>
                  </div>
                  <p>{pack.summary}</p>
                  <p className="muted">Источников: {pack.items.length} · {formatDateTime(pack.created_at)}</p>
                  {pack.gaps.length > 0 && <ul>{pack.gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul>}
                  <details>
                    <summary>Использованные источники и выдержки</summary>
                    <ul>{pack.items.map((item) => <li key={item.result_key}><strong>{item.source_title}</strong>{item.filename && ` · ${item.filename}`}<p>{item.excerpt}</p></li>)}</ul>
                  </details>
                </article>
              ))}
            </div>
          ) : <p className="empty-state">{campaignId ? "Для этой кампании пока нет сохранённых пакетов исследований." : "Пакетов знаний пока нет."}</p>}
        </section>

        {user?.role === "ADMIN" && !store && (
          <button onClick={() => void initialize()} disabled={activeAction !== null}>
            {activeAction === "initialize" ? "Подключаем базу знаний…" : "Инициализировать базу знаний"}
          </button>
        )}
        {user?.role === "ADMIN" && (
          <form onSubmit={upload}>
            <h2>Загрузить документ</h2>
            <label htmlFor="knowledge-file">Файл PDF, DOCX, TXT или MD</label>
            <input id="knowledge-file" type="file" accept=".pdf,.docx,.txt,.md" onChange={(event) => setFile(event.target.files?.[0] ?? null)} required />
            <label htmlFor="knowledge-title">Название (необязательно)</label>
            <input id="knowledge-title" value={title} onChange={(event) => setTitle(event.target.value)} />
            <label htmlFor="knowledge-author">Автор (необязательно)</label>
            <input id="knowledge-author" value={author} onChange={(event) => setAuthor(event.target.value)} />
            <button disabled={activeAction !== null}>{activeAction === "upload" ? "Загружаем документ…" : "Загрузить документ"}</button>
            {activeAction === "upload" && <p role="status">Документ загружается и передаётся на индексацию…</p>}
          </form>
        )}

        <section className="page-section" aria-labelledby="knowledge-items-heading">
          <div className="section-heading">
            <div><p className="eyebrow">Документы</p><h2 id="knowledge-items-heading">Материалы базы знаний</h2></div>
          </div>
          {items.length === 0 ? <p className="empty-state">Документов пока нет.</p> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Название</th><th>Источник</th><th>Тип</th><th>Статус</th><th>Размер</th><th>Загружен</th><th>Индексирован</th><th>Действия</th></tr></thead>
                <tbody>{items.map((item) => (
                  <tr key={item.id}>
                    <td>{item.title}<br /><small>{item.original_filename ?? item.source_url ?? "Имя файла не указано"}</small>{item.error_message && <details><summary>Проблема индексации</summary><p className="error">{item.error_message}</p>{item.error_code && <p>Код: <code>{item.error_code}</code></p>}</details>}</td>
                    <td>{sources.find((source) => source.id === item.source_id)?.name ?? "Источник не указан"}</td>
                    <td>{item.content_type.toUpperCase()}</td>
                    <td>{STATUS_LABELS[item.status]}</td>
                    <td>{item.file_size_bytes == null ? "—" : `${Math.ceil(item.file_size_bytes / 1024)} КБ`}</td>
                    <td>{formatDateTime(item.created_at)}</td>
                    <td>{formatDateTime(item.indexed_at)}</td>
                    <td>
                      {user?.role === "ADMIN" && item.status === "FAILED" && <button className="secondary" disabled={itemBusyId !== null} onClick={() => void retry(item)}>{itemBusyId === item.id ? "Повторяем…" : "Повторить"}</button>}
                      {user?.role === "ADMIN" && item.status !== "ARCHIVED" && <button className="secondary" disabled={itemBusyId !== null} onClick={() => void archive(item)}>{itemBusyId === item.id ? "Обновляем…" : "Архивировать"}</button>}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </section>

        <form onSubmit={search}>
          <h2>Поиск и просмотр выдержек</h2>
          <p className="muted">Поиск запускается только после отправки запроса. Откройте результат, чтобы прочитать извлечённый фрагмент и увидеть его источник.</p>
          <label htmlFor="knowledge-query">Запрос</label>
          <input id="knowledge-query" value={query} onChange={(event) => setQuery(event.target.value)} required />
          <button disabled={activeAction !== null}>{activeAction === "search" ? "Ищем…" : "Найти"}</button>
          {activeAction === "search" && <p role="status">Ищем подходящие выдержки…</p>}
        </form>
        {results.length > 0 && (
          <section className="search-results" aria-label="Выдержки из базы знаний">
            {results.map((result) => (
              <article className="agent-card" key={`${result.file_id}-${result.excerpt}`}>
                <h3>{result.source_title}</h3>
                <p><strong>Файл:</strong> {result.filename}</p>
                <p><strong>Релевантность:</strong> {result.score == null ? "не указана" : result.score.toFixed(3)}</p>
                <p>{result.excerpt}</p>
              </article>
            ))}
          </section>
        )}
      </section>
    </main>
  );
}

export default function KnowledgePage() {
  return <Suspense fallback={<main><p role="status">Загружаем базу знаний…</p></main>}><KnowledgePageContent /></Suspense>;
}
