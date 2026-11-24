"use client";

import { useCallback, useEffect, useState } from "react";
import { studioApi, type StudioGenerationJob } from "@/lib/studio";

const POLL_INTERVAL = 2_000;
const MAX_POLLS = 60;
const STATUS_LABEL: Record<StudioGenerationJob["status"], string> = {
  PENDING: "Генерация поставлена в очередь.",
  RUNNING: "Иллюстрация генерируется.",
  SUCCEEDED: "Черновик иллюстрации готов.",
  FAILED: "Генерация завершилась ошибкой.",
  UNKNOWN: "Результат генерации не подтверждён.",
};

function isPending(job?: StudioGenerationJob) {
  return !job || job.status === "PENDING" || job.status === "RUNNING";
}

export function useStudioGeneration(campaignId: string, onSucceeded: () => void) {
  const storageKey = `iteam:creative-generation:${campaignId}`;
  const [jobId, setJobId] = useState<string>();
  const [job, setJob] = useState<StudioGenerationJob>();
  const [stopped, setStopped] = useState(false);
  const active = !!jobId && isPending(job);

  useEffect(() => {
    // Read only after the client has hydrated.
    const restore = window.setTimeout(() => {
      try {
        const previousJobId = sessionStorage.getItem(storageKey);
        if (previousJobId) setJobId((current) => current ?? previousJobId);
      } catch {
        // Polling in this tab still works if storage is unavailable.
      }
    }, 0);
    return () => window.clearTimeout(restore);
  }, [storageKey]);

  const update = useCallback((next: StudioGenerationJob) => {
    setJobId(next.id);
    setJob(next);
    setStopped(false);
    try {
      if (isPending(next)) sessionStorage.setItem(storageKey, next.id);
      else sessionStorage.removeItem(storageKey);
    } catch {
      // The current generation is tracked in state when storage is unavailable.
    }
    if (next.status === "SUCCEEDED") onSucceeded();
  }, [onSucceeded, storageKey]);

  useEffect(() => {
    if (!jobId || !active || stopped) return;
    const controller = new AbortController();
    let timeout: number | undefined;
    let polls = 0;

    async function poll(id: string) {
      try {
        const next = await studioApi.generationJob(campaignId, id, controller.signal);
        if (controller.signal.aborted) return;
        if (next.id !== id) throw new Error("Статус получен для другой задачи.");
        update(next);
        if (isPending(next)) {
          polls += 1;
          if (polls >= MAX_POLLS) setStopped(true);
          else timeout = window.setTimeout(() => void poll(id), POLL_INTERVAL);
        }
      } catch {
        if (!controller.signal.aborted) setStopped(true);
      }
    }

    void poll(jobId);
    return () => {
      controller.abort();
      if (timeout !== undefined) window.clearTimeout(timeout);
    };
  }, [active, campaignId, jobId, stopped, update]);

  return {
    job,
    active,
    label: job ? STATUS_LABEL[job.status] : jobId ? "Проверяем задачу генерации…" : undefined,
    stopped,
    update,
    resume: () => setStopped(false),
  };
}
