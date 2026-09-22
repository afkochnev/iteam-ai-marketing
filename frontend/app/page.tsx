"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import Link from "next/link";
import { useAuth } from "@/components/auth-provider";
import { systemApi, SystemStatus } from "@/lib/api";
import { useState } from "react";

export default function Home() {
  const { user, loading, logout } = useAuth();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const router = useRouter();
  useEffect(() => { if (!loading && !user) router.replace("/login"); }, [loading, user, router]);
  useEffect(() => {
    if (!user || user.role !== "ADMIN") return;
    systemApi.status().then(setStatus).catch((error: Error) => setStatusError(error.message));
  }, [user]);
  if (loading || !user) return <main><p>Проверяем авторизацию…</p></main>;
  async function handleLogout() { await logout(); router.replace("/login"); }
  return <main><section><p className="eyebrow">Dashboard</p><h1>AI Marketing Department</h1><dl>
    <div><dt>Вы вошли как:</dt><dd>{user.full_name ?? user.email}</dd></div>
    <div><dt>Email:</dt><dd>{user.email}</dd></div><div><dt>Роль:</dt><dd>{user.role}</dd></div>
  </dl><nav><Link href="/campaigns">Кампании</Link> <Link href="/tasks">Задачи</Link> <Link href="/agents">Агенты</Link> <Link href="/knowledge">База знаний</Link> <Link href="/content">Контент</Link> <Link href="/approvals">Согласования</Link></nav>
  {user.role === "ADMIN" && <section aria-label="Операционный статус"><h2>Операционный статус</h2>{statusError ? <p role="alert">Не удалось загрузить статус: {statusError}</p> : !status ? <p>Загрузка статуса…</p> : <dl><div><dt>Готовые задачи</dt><dd>{status.tasks.READY ?? 0}</dd></div><div><dt>Выполняются</dt><dd>{status.tasks.IN_PROGRESS ?? 0}</dd></div><div><dt>Ошибки</dt><dd>{status.tasks.FAILED ?? 0}</dd></div><div><dt>Зависшие задачи</dt><dd>{status.stuck_tasks}</dd></div><div><dt>Ожидают согласования</dt><dd>{status.pending_approvals}</dd></div></dl>}{status && !status.stuck_tasks && !status.tasks.FAILED && <p>Проблем не обнаружено.</p>}</section>}
  <button onClick={handleLogout}>Выйти</button></section></main>;
}
