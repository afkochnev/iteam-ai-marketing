"use client";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/components/auth-provider";
import { Campaign, campaignsApi } from "@/lib/api";
import { ChatMessage, Conversation, directorChatApi, verifiedHref } from "@/lib/director-chat";

export default function DirectorPage() {
  const { id } = useParams<{ id: string }>(); const router = useRouter(); const { user, loading } = useAuth();
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState<string>(""); const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [content, setContent] = useState(""); const [error, setError] = useState("");
  const [busy, setBusy] = useState(false); const [historyLoading, setHistoryLoading] = useState(false);
  const attempt = useRef<{ content: string; id: string } | null>(null);
  const selection = useRef(selected);
  useEffect(() => { selection.current = selected; }, [selected]);
  const active = conversations.find((item) => item.id === selected);
  const readOnly = campaign?.status === "ARCHIVED" || Boolean(active?.archived_at);
  const pending = messages.some((item) => item.status === "PENDING");
  const showError = (reason: unknown) => setError(reason instanceof Error ? reason.message : "Не удалось загрузить диалог.");
  const refresh = useCallback(async (conversation: string) => {
    const result = await directorChatApi.messages(conversation);
    if (selection.current === conversation) setMessages(result);
  }, []);
  useEffect(() => { if (!loading && !user) router.replace("/login"); }, [loading, user, router]);
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    Promise.all([campaignsApi.get(id), directorChatApi.list(id)]).then(([value, list]) => {
      if (cancelled) return; setCampaign(value); setConversations(list); setMessages([]); setHistoryLoading(Boolean(list[0])); setSelected(list[0]?.id ?? "");
    }).catch((reason) => { if (!cancelled) showError(reason); });
    return () => { cancelled = true; };
  }, [id, user]);
  useEffect(() => {
    if (!selected) return;
    let cancelled = false; attempt.current = null;
    refresh(selected).catch((reason) => { if (!cancelled) showError(reason); }).finally(() => { if (!cancelled) setHistoryLoading(false); });
    return () => { cancelled = true; };
  }, [selected, refresh]);
  useEffect(() => {
    if (!pending || !selected) return;
    // Schedule after each completed read, avoiding overlapping polling requests.
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { await refresh(selected); } catch (reason) { if (!cancelled) showError(reason); }
      if (!cancelled) timer = setTimeout(() => void poll(), 2000);
    }
    timer = setTimeout(() => void poll(), 2000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [pending, selected, refresh]);
  async function create() {
    setBusy(true); setError("");
    try { const value = await directorChatApi.create(id); setConversations((items) => [value, ...items]); setMessages([]); setHistoryLoading(true); setSelected(value.id); }
    catch (reason) { showError(reason); } finally { setBusy(false); }
  }
  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!content.trim() || pending || busy || readOnly || historyLoading) return;
    setBusy(true); setError("");
    if (attempt.current?.content !== content) attempt.current = { content, id: crypto.randomUUID() };
    try {
      const value = await directorChatApi.send(selected, content, attempt.current.id);
      setMessages((items) => [...items.filter((item) => item.id !== value.user_message.id && item.id !== value.assistant_message.id), value.user_message, value.assistant_message]);
      setContent(""); attempt.current = null;
    } catch (reason) { showError(reason); } finally { setBusy(false); }
  }
  async function retry(message: string) {
    setBusy(true); setError("");
    try { const value = await directorChatApi.retry(selected, message); setMessages((items) => items.map((item) => item.id === value.id ? value : item)); }
    catch (reason) { showError(reason); } finally { setBusy(false); }
  }
  async function archive() {
    setBusy(true); setError("");
    try { const value = await directorChatApi.archive(selected); setConversations((items) => items.map((item) => item.id === value.id ? value : item)); }
    catch (reason) { showError(reason); } finally { setBusy(false); }
  }
  if (loading || !user) return <div><p>Загрузка…</p></div>;
  return <div className="wide"><Link href={`/campaigns/${id}`}>К кампании</Link><h1>{campaign?.name ?? "Кампания"} · Marketing Director</h1>
    <p className="notice">Консультативный режим. Директор анализирует состояние кампании, но не изменяет данные без отдельного подтверждённого workflow.</p>
    {error && <p role="alert" className="error">{error}</p>}
    <div className="director-chat-layout"><aside className="card"><h2>Диалоги</h2><button onClick={create} disabled={busy || !campaign || campaign.status === "ARCHIVED"}>Новый диалог</button>
      <ul>{conversations.map((item, index) => <li key={item.id}><button className="secondary" aria-pressed={selected === item.id} disabled={busy || selected === item.id} onClick={() => { setContent(""); setMessages([]); setHistoryLoading(true); setError(""); setSelected(item.id); }}>{item.title || `Диалог ${conversations.length - index}`}{item.archived_at ? " · Архив" : ""}</button></li>)}</ul>
    </aside><section className="card" aria-label="История диалога">
      {historyLoading && <p>Загрузка истории…</p>}{!selected && <p>Создайте диалог, чтобы обсудить состояние кампании.</p>}
      {messages.map((item) => <article key={item.id} className={`chat-message chat-${item.role.toLowerCase()}`}><h3>{item.role === "USER" ? "Вы" : item.role === "ASSISTANT" ? "Директор по маркетингу" : "Система"}</h3>
        {item.status === "PENDING" ? <p role="status">Директор готовит ответ…</p> : item.status === "FAILED" ? <><p role="alert">{item.error_message || "Не удалось получить ответ."}</p><button disabled={busy || pending || readOnly} onClick={() => retry(item.id)}>Повторить ответ</button></> : <p className="chat-text">{item.content}</p>}
        {item.references?.length > 0 && <aside aria-label="Проверенные ссылки"><h4>Объекты кампании</h4><ul>{item.references.map((reference, index) => { const href = verifiedHref(reference, id); return href ? <li key={index}><Link href={href}>{reference.label}</Link></li> : null; })}</ul></aside>}
        {item.limitations?.length > 0 && <aside className="chat-limitations"><h4>Ограничения ответа</h4><ul>{item.limitations.map((value, index) => <li key={index}>{value}</li>)}</ul></aside>}
      </article>)}
      {selected && <><form onSubmit={send}><label htmlFor="director-message">Ваш вопрос</label><textarea id="director-message" maxLength={6000} value={content} disabled={readOnly || busy || historyLoading} onChange={(event) => setContent(event.target.value)} /><button disabled={!content.trim() || busy || pending || readOnly || historyLoading}>Отправить</button></form>
        {readOnly && <p>Архив доступен только для чтения.</p>}{!active?.archived_at && <button className="secondary" disabled={busy || pending} onClick={archive}>Архивировать диалог</button>}</>}
    </section></div></div>;
}
