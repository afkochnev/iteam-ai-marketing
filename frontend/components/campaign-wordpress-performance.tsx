"use client";

import { useEffect, useState } from "react";
import { FriendlyError } from "@/components/friendly-error";
import { metricsApi, type WordPressPerformance } from "@/lib/api";

export function CampaignWordPressPerformance({ campaignId }: { campaignId: string }) {
  const [result, setResult] = useState<WordPressPerformance | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [requestVersion, setRequestVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void metricsApi.wordpress(campaignId, controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) setResult(value);
      },
      (reason: unknown) => {
        if (!controller.signal.aborted) setError(reason);
      },
    );
    return () => controller.abort();
  }, [campaignId, requestVersion]);

  if (error == null && !result?.placements.length) return null;

  return <section aria-label="Страницы WordPress">
    <h3>Страницы WordPress</h3>
    {error != null ? <FriendlyError
      error={error}
      fallback="Не удалось загрузить страницы WordPress."
      onRetry={() => {
        setError(null);
        setRequestVersion((version) => version + 1);
      }}
    /> : result && <>
      {result.analytics_status === "not_configured" && <p>Сбор веб-метрик пока не подключён.</p>}
      <div className="table-wrap">
        <table aria-label="Страницы WordPress">
          <thead><tr>
            <th scope="col">Статья</th>
            <th scope="col">Подтверждённая версия</th>
            <th scope="col">Сайт</th>
            <th scope="col">ID WordPress</th>
          </tr></thead>
          <tbody>{result.placements.map((placement) => <tr key={placement.placement_id}>
            <td><a href={placement.canonical_url} target="_blank" rel="noreferrer">{placement.current_title}</a></td>
            <td title={placement.current_version_id}>v{placement.current_version_number} · {placement.current_version_id.slice(0, 8)}</td>
            <td>{placement.site_url}</td>
            <td><code>{placement.post_id}</code></td>
          </tr>)}</tbody>
        </table>
      </div>
    </>}
  </section>;
}
