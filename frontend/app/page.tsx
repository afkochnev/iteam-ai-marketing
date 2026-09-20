"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import Link from "next/link";
import { useAuth } from "@/components/auth-provider";

export default function Home() {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  useEffect(() => { if (!loading && !user) router.replace("/login"); }, [loading, user, router]);
  if (loading || !user) return <main><p>Проверяем авторизацию…</p></main>;
  async function handleLogout() { await logout(); router.replace("/login"); }
  return <main><section><p className="eyebrow">Dashboard</p><h1>AI Marketing Department</h1><dl>
    <div><dt>Вы вошли как:</dt><dd>{user.full_name ?? user.email}</dd></div>
    <div><dt>Email:</dt><dd>{user.email}</dd></div><div><dt>Роль:</dt><dd>{user.role}</dd></div>
  </dl><nav><Link href="/agents">Агенты</Link></nav><button onClick={handleLogout}>Выйти</button></section></main>;
}
