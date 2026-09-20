"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { agentsApi, type AgentListItem } from "@/lib/api";
import { agentDisplayName } from "@/lib/agents";

export default function AgentsPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [agents, setAgents] = useState<AgentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!authLoading && !user) { router.replace("/login"); return; }
    if (user) agentsApi.list().then(setAgents).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false));
  }, [authLoading, user, router]);

  if (authLoading || loading) return <main><p>Загружаем агентов…</p></main>;
  if (error) return <main><section><h1>Агенты</h1><p role="alert" className="error">{error}</p></section></main>;

  return <main><section className="wide"><header className="page-header"><div><p className="eyebrow">Команда</p><h1>Агенты</h1></div><Link href="/">На главную</Link></header>
    {agents.length === 0 ? <p>Агенты пока не созданы.</p> : <div className="agent-grid">{agents.map((agent) =>
      <Link className="agent-card" href={`/agents/${agent.id}`} key={agent.id}>
        <h2>{agentDisplayName(agent.slug, agent.name)}</h2><p>{agent.name}</p><p>{agent.description}</p>
        <dl><div><dt>Статус</dt><dd>{agent.status === "ACTIVE" ? "Активен" : "Неактивен"}</dd></div>
          <div><dt>Автономность</dt><dd>{agent.autonomy_level}</dd></div><div><dt>Модель</dt><dd>{agent.model ?? "Модель по умолчанию"}</dd></div></dl>
      </Link>)}</div>}
  </section></main>;
}
