"use client";
import { PageBreadcrumbs } from "@/components/page-breadcrumbs";
import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth-provider";
import { authApi } from "@/lib/api";
import { PASSWORD_REQUIREMENTS, passwordError } from "@/lib/password-policy";

export default function ProfilePage() {
  const { user, loading } = useAuth(); const router = useRouter();
  const [savedEmail, setSavedEmail] = useState<string | null>(null);
  const [email, setEmail] = useState(""); const [emailPassword, setEmailPassword] = useState(""); const [emailNotice, setEmailNotice] = useState(""); const [emailError, setEmailError] = useState("");
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
  async function updateEmail(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setEmailError(""); setEmailNotice(""); setBusy(true);
    const submittedEmail = email.trim().toLowerCase();
    try { const result = await authApi.changeEmail(submittedEmail, emailPassword); setEmailNotice(result.message); setSavedEmail(submittedEmail); setEmailPassword(""); }
    catch (reason) { setEmailError(reason instanceof Error ? reason.message : "Не удалось изменить email."); }
    finally { setBusy(false); }
  }
  if (loading || !user) return <main><p>Загрузка…</p></main>;
  return <main><PageBreadcrumbs items={[{ label: "Главная", href: "/" }, { label: "Профиль" }]} /><h1>Профиль</h1><section className="card"><p>{user.full_name}</p><p>{savedEmail ?? user.email}</p><p>{user.role}</p></section>
    {user.role === "ADMIN" && <section className="login-card"><h2>Изменить email для входа</h2><p>Пароль и права администратора сохранятся. После изменения используйте новый email.</p><form onSubmit={updateEmail}><label htmlFor="new-email">Новый email</label><input id="new-email" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} /><label htmlFor="email-password">Текущий пароль для подтверждения email</label><input id="email-password" type="password" autoComplete="current-password" required value={emailPassword} onChange={(event) => setEmailPassword(event.target.value)} /><button disabled={busy}>Изменить email</button></form>{emailError && <p role="alert">{emailError}</p>}{emailNotice && <p role="status">{emailNotice}</p>}</section>}
    <section className="login-card"><h2>Сменить пароль</h2><p>{PASSWORD_REQUIREMENTS}</p><form onSubmit={submit}>
      <label htmlFor="current">Текущий пароль</label><input id="current" type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} />
      <label htmlFor="password">Новый пароль</label><input id="password" type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} />
      <label htmlFor="confirmation">Подтверждение пароля</label><input id="confirmation" type="password" autoComplete="new-password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
      <button disabled={busy}>Изменить пароль</button></form>{error && <p role="alert" className="error">{error}</p>}{notice && <p role="status">{notice}</p>}
    </section></main>;
}
