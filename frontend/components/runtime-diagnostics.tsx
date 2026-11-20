"use client";

import { useEffect, useRef, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { systemApi, type RuntimeProcess, type RuntimeSnapshot } from "@/lib/api";
import { formatDateTime } from "@/lib/campaigns";

const processLabels: Record<string, string> = {
  "worker:ai": "AI-исполнитель",
  "worker:ai_control": "Диспетчер AI",
  "worker:publication": "Исполнитель публикаций",
  "worker:publication_control": "Диспетчер публикаций",
  "worker:metrics": "Сбор метрик",
  "scheduler:ai": "Планировщик AI",
  "scheduler:publication": "Планировщик публикаций",
};

const brokerLabels: Record<RuntimeSnapshot["broker_state"], string> = {
  ok: "Данные брокера прочитаны.",
  partial: "Часть диагностических данных недоступна.",
  unavailable: "Брокер недоступен. Состояние очередей и сигналов неизвестно.",
  unsupported: "Этот режим брокера не поддерживает диагностику очередей и сигналов.",
  disabled: "Диагностика очередей и исполнителей отключена.",
};

function signalLabel(process: RuntimeProcess, expired: boolean): string {
  if (expired && process.state === "recent") return "Свежесть сигнала не подтверждена";
  return {
    recent: "Свежий сигнал",
    stale: "Сигнал устарел",
    unobserved: "Сигнал не наблюдался",
    unknown: "Состояние неизвестно",
  }[process.state];
}

function RuntimeDiagnosticsPanel() {
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [expired, setExpired] = useState(false);
  const requestId = useRef(0);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => {
    requestId.current += 1;
    controller.current?.abort();
  }, []);

  useEffect(() => {
    if (!snapshot) return;
    const seconds = snapshot.stale_after_seconds;
    let expirySeconds = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
    for (const process of snapshot.processes) {
      if (process.state !== "recent") continue;
      const age = process.age_seconds;
      const remaining = typeof age === "number" && Number.isFinite(age) && age >= 0
        ? Math.max(0, seconds - age) : 0;
      expirySeconds = Math.min(expirySeconds, remaining);
    }
    const timer = window.setTimeout(() => setExpired(true), expirySeconds * 1000);
    return () => window.clearTimeout(timer);
  }, [snapshot]);

  async function check() {
    const id = ++requestId.current;
    controller.current?.abort();
    const nextController = new AbortController();
    controller.current = nextController;
    setBusy(true);
    setError("");
    setSnapshot(null);
    setExpired(false);
    try {
      const result = await systemApi.runtime(nextController.signal);
      if (id === requestId.current) setSnapshot(result);
    } catch {
      if (id === requestId.current) setError("Диагностика недоступна. Повторите проверку.");
    } finally {
      if (id === requestId.current) {
        controller.current = null;
        setBusy(false);
      }
    }
  }

  return <section className="section-block" aria-labelledby="runtime-diagnostics-heading">
    <h2 id="runtime-diagnostics-heading">Исполнители и очереди</h2>
    <p className="subtle">Проверка показывает очереди и сигналы исполнителей на момент наблюдения. Доступ к внешним каналам и возможность отправки требуют отдельной проверки.</p>
    <button className="secondary" disabled={busy} onClick={() => void check()}>
      {busy ? "Проверяем исполнителей и очереди…" : "Проверить исполнителей и очереди"}
    </button>
    {!snapshot && !busy && !error && <p>Запустите проверку, чтобы получить снимок состояния.</p>}
    {busy && <p role="status">Получаем диагностический снимок…</p>}
    {error && <p role="alert">{error}</p>}
    {snapshot && <>
      <p>Снимок проверки: <time dateTime={snapshot.sampled_at}>{formatDateTime(snapshot.sampled_at)}</time></p>
      <p role="status">{brokerLabels[snapshot.broker_state]}</p>
      {expired && <p role="alert">Срок проверки истёк. Обновите данные; свежесть сигналов и количество ожидающих сообщений сейчас не подтверждены.</p>}
      <div style={{ overflowX: "auto" }}>
        <table aria-label="Очереди брокера">
          <thead><tr><th scope="col">Очередь</th><th scope="col">Ожидающих сообщений</th></tr></thead>
          <tbody>{snapshot.queues.map((queue) => <tr key={queue.name}>
            <th scope="row"><code>{queue.name}</code></th>
            <td>{expired ? "—" : queue.waiting_messages ?? "—"}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table aria-label="Сигналы исполнителей и планировщиков">
          <thead><tr><th scope="col">Роль</th><th scope="col">Сигнал</th><th scope="col">Последний сигнал</th><th scope="col">Наблюдалось / свежих</th><th scope="col">Очереди исполнителя</th></tr></thead>
          <tbody>{snapshot.processes.map((process) => <tr key={process.id}>
            <th scope="row">{processLabels[process.id] ?? process.role}</th>
            <td>{signalLabel(process, expired)}{process.build_mismatch === true && <p className="warning">Сигнал относится к другой сборке.</p>}</td>
            <td>{formatDateTime(process.last_seen_at)}{process.age_seconds !== null && <p className="subtle">{Math.floor(process.age_seconds)} с до проверки</p>}</td>
            <td>{expired ? "—" : process.observed_instances ?? "—"} / {expired ? "—" : process.fresh_instances ?? "—"}</td>
            <td>{process.queues.length ? <code>{process.queues.join(", ")}</code> : "—"}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </>}
  </section>;
}

export function RuntimeDiagnostics() {
  const { user, loading } = useAuth();
  if (loading || user?.role !== "ADMIN") return null;
  return <RuntimeDiagnosticsPanel key={user.id} />;
}
