"use client";
import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth-provider";
import { authApi } from "@/lib/api";
import { PASSWORD_REQUIREMENTS, passwordError } from "@/lib/password-policy";

export default function ProfilePage() {
  const { user, loading } = useAuth(); const router = useRouter();
  const [current, setCurrent] = useState(""); const [password, setPassword] = useState(""); const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [busy, setBusy] = useState(false);
  useEffect(() => { if (!loading && !user) router.replace("/login"); }, [loading, user, router]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); setNotice("");
    const validation = passwordError(password, confirmation);
    if (!current) { setError("Введите текущий пароль."); return; }
    if (validation) { setError(validation); return; }
    if (current === password) { setError("Новый пароль должен отличаться от текущего."); return; }
    setBusy(true);
    try { const result = await authApi.changePassword(current, password); setNotice(result.message); setCurrent(""); setPassword(""); setConfirmation(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось изменить пароль."); }
    finally { setBusy(false); }
  }
  if (loading || !user) return <main><p>Загрузка…</p></main>;
  return <main><h1>Профиль</h1><section className="card"><p>{user.full_name}</p><p>{user.email}</p><p>{user.role}</p></section>
    <section className="login-card"><h2>Сменить пароль</h2><p>{PASSWORD_REQUIREMENTS}</p><form onSubmit={submit}>
      <label htmlFor="current">Текущий пароль</label><input id="current" type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} />
      <label htmlFor="password">Новый пароль</label><input id="password" type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} />
      <label htmlFor="confirmation">Подтверждение пароля</label><input id="confirmation" type="password" autoComplete="new-password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
      <button disabled={busy}>Изменить пароль</button></form>{error && <p role="alert" className="error">{error}</p>}{notice && <p role="status">{notice}</p>}
    </section></main>;
}
