"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { agentsApi, type Agent, type AgentStatus } from "@/lib/api";
import { agentDisplayName } from "@/lib/agents";

export default function AgentDetailsPage() {
  const { id } = useParams<{ id: string }>(); const router = useRouter();
  const { user, loading: authLoading } = useAuth();
  const [agent, setAgent] = useState<Agent | null>(null); const [editing, setEditing] = useState(false);
  const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const [description, setDescription] = useState(""); const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState(""); const [status, setStatus] = useState<AgentStatus>("ACTIVE");
  const [autonomy, setAutonomy] = useState(2);

  useEffect(() => {
    if (!authLoading && !user) { router.replace("/login"); return; }
    if (user) agentsApi.get(id).then((value) => { setAgent(value); setDescription(value.description ?? ""); setPrompt(value.system_prompt); setModel(value.model ?? ""); setStatus(value.status); setAutonomy(value.autonomy_level); }).catch((reason: Error) => setError(reason.message));
  }, [authLoading, user, id, router]);

  async function save(event: FormEvent) {
    event.preventDefault(); setError(""); setNotice("");
    try { const value = await agentsApi.update(id, { description, system_prompt: prompt, model, status, autonomy_level: autonomy }); setAgent(value); setEditing(false); setNotice("Изменения сохранены."); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить изменения."); }
  }

  async function updateTool(toolId: string, field: "is_enabled" | "requires_approval", value: boolean) {
    if (!agent) return; setError("");
    try { const changed = await agentsApi.updateTool(id, toolId, { [field]: value }); setAgent({ ...agent, tools: agent.tools.map((tool) => tool.id === toolId ? changed : tool) }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось изменить разрешение."); }
  }

  if (authLoading || (!agent && !error)) return <main><p>Загружаем агента…</p></main>;
  if (!agent) return <main><section><p role="alert" className="error">{error}</p><Link href="/agents">К агентам</Link></section></main>;
  const isAdmin = user?.role === "ADMIN";

  return <main><section className="wide"><header className="page-header"><div><p className="eyebrow">{agent.slug}</p><h1>{agentDisplayName(agent.slug, agent.name)}</h1></div><Link href="/agents">К агентам</Link></header>
    {notice && <p className="notice">{notice}</p>}{error && <p role="alert" className="error">{error}</p>}
    {!editing ? <><div className="details"><p><strong>Роль:</strong> {agent.role}</p><p><strong>Описание:</strong> {agent.description}</p><p><strong>Модель:</strong> {agent.model ?? "Модель по умолчанию"}</p><p><strong>Статус:</strong> {agent.status === "ACTIVE" ? "Активен" : "Неактивен"}</p><p><strong>Уровень автономности:</strong> {agent.autonomy_level}</p></div><h2>System prompt</h2><pre>{agent.system_prompt}</pre>{isAdmin && <button onClick={() => setEditing(true)}>Редактировать</button>}</>
      : <form onSubmit={save}><label htmlFor="description">Описание</label><textarea id="description" value={description} onChange={(e) => setDescription(e.target.value)} /><label htmlFor="prompt">System prompt</label><textarea id="prompt" className="prompt" value={prompt} onChange={(e) => setPrompt(e.target.value)} required /><label htmlFor="model">Модель</label><input id="model" value={model} onChange={(e) => setModel(e.target.value)} placeholder="Модель по умолчанию" /><label htmlFor="status">Статус</label><select id="status" value={status} onChange={(e) => setStatus(e.target.value as AgentStatus)}><option value="ACTIVE">Активен</option><option value="INACTIVE">Неактивен</option></select><label htmlFor="autonomy">Уровень автономности</label><input id="autonomy" type="number" min="0" max="5" value={autonomy} onChange={(e) => setAutonomy(Number(e.target.value))} /><div><button type="submit">Сохранить</button> <button type="button" className="secondary" onClick={() => setEditing(false)}>Отмена</button></div></form>}
    <h2>Инструменты</h2><div className="tools">{agent.tools.map((tool) => <div className="tool" key={tool.id}><code>{tool.tool_name}</code><label><input type="checkbox" checked={tool.is_enabled} disabled={!isAdmin} onChange={(e) => updateTool(tool.id, "is_enabled", e.target.checked)} /> Включён</label><label><input type="checkbox" checked={tool.requires_approval} disabled={!isAdmin} onChange={(e) => updateTool(tool.id, "requires_approval", e.target.checked)} /> Требует согласования</label></div>)}</div>
  </section></main>;
}
