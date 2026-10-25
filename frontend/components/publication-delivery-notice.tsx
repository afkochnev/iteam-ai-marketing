"use client";

import { useState } from "react";
import { publicationsApi } from "@/lib/api";

export function PublicationDeliveryNotice({ providerEnabled }: { providerEnabled?: boolean }) {
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState(false);
  async function check() {
    setBusy(true);
    setResult("");
    try { setResult((await publicationsApi.deliveryStatus()).message); }
    catch { setResult("Проверка недоступна. План сохранён. Попробуйте проверить сервис позже."); }
    finally { setBusy(false); }
  }
  return <aside className="delivery-status" aria-label="Состояние сервиса отправки">
    <p className={providerEnabled === false ? "warning" : "subtle"} role="status">{providerEnabled === false
      ? "Автоматическая отправка отключена. План сохранён; перед назначенным временем потребуется включить доставку."
      : providerEnabled === true
        ? "Автоматическая отправка включена. Проверка готовности сервиса — отдельное действие; планирование завершено."
        : "Проверка недоступна. Состояние сервиса отправки сейчас неизвестно. План сохранён."}</p>
    <button className="secondary" disabled={busy} onClick={() => void check()}>{busy ? "Проверяем сервис…" : "Проверить сервис отправки"}</button>
    {result && <p className="subtle" role="status">{result}</p>}
  </aside>;
}
