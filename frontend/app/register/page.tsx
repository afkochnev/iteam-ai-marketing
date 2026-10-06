"use client";
import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { authApi } from "@/lib/api";
import { PASSWORD_REQUIREMENTS, passwordError } from "@/lib/password-policy";

export default function RegisterPage() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [email, setEmail] = useState(""); const [name, setName] = useState("");
  const [password, setPassword] = useState(""); const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState(""); const [success, setSuccess] = useState(false); const [busy, setBusy] = useState(false);
  useEffect(() => { authApi.capabilities().then((value) => setEnabled(value.self_registration_enabled)).catch(() => setError("Не удалось проверить доступность регистрации. Обновите страницу.")); }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError("");
    const validation = passwordError(password, confirmation);
    if (!email.trim() || !name.trim()) { setError("Введите email и имя."); return; }
    if (validation) { setError(validation); return; }
    setBusy(true);
    try { await authApi.register(email.trim().toLowerCase(), password, name.trim()); setSuccess(true); setPassword(""); setConfirmation(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось зарегистрироваться."); }
    finally { setBusy(false); }
  }
  return <main><section className="login-card"><h1>Регистрация</h1>
    {enabled === false && <p role="status">Самостоятельная регистрация отключена. Обратитесь к администратору.</p>}
    {enabled === null && !error && <p role="status">Проверяем доступность регистрации…</p>}
    {success ? <p role="status">Регистрация завершена. <Link href="/login">Войти с новым паролем</Link></p> : enabled && <form onSubmit={submit}>
      <label htmlFor="email">Email</label><input id="email" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} />
      <label htmlFor="name">Полное имя</label><input id="name" autoComplete="name" maxLength={255} required value={name} onChange={(event) => setName(event.target.value)} />
      <p>{PASSWORD_REQUIREMENTS}</p><label htmlFor="password">Пароль</label><input id="password" type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} />
      <label htmlFor="confirmation">Подтверждение пароля</label><input id="confirmation" type="password" autoComplete="new-password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
      <button disabled={busy}>Зарегистрироваться</button>
    </form>}{error && <p className="error" role="alert">{error}</p>}<p><Link href="/login">К входу</Link></p>
  </section></main>;
}
