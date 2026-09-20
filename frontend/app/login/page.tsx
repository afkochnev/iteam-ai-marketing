"use client";
import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth-provider";

export default function LoginPage() {
  const { user, loading, login } = useAuth(); const router = useRouter();
  const [email, setEmail] = useState(""); const [password, setPassword] = useState("");
  const [error, setError] = useState(""); const [submitting, setSubmitting] = useState(false);
  useEffect(() => { if (!loading && user) router.replace("/"); }, [loading, user, router]);
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError("");
    if (!email.trim() || !password) { setError("Введите email и пароль."); return; }
    setSubmitting(true);
    try { await login(email, password); router.replace("/"); }
    catch (loginError) { setError(loginError instanceof Error ? loginError.message : "Не удалось войти."); }
    finally { setSubmitting(false); }
  }
  return <main><section className="login-card"><p className="eyebrow">iTeam</p><h1>Вход</h1>
    <form onSubmit={handleSubmit} noValidate><label htmlFor="email">Email</label>
      <input id="email" name="email" type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} />
      <label htmlFor="password">Пароль</label><input id="password" name="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
      {error && <p role="alert" className="error">{error}</p>}<button type="submit" disabled={submitting}>{submitting ? "Входим…" : "Войти"}</button>
    </form></section></main>;
}
