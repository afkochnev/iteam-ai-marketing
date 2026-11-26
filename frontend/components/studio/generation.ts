"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
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

function shouldStoreJobId(job: StudioGenerationJob) {
  return isPending(job) || ((job.status === "FAILED" || job.status === "UNKNOWN") && job.recovery !== null);
}

function storeTrackedJob(key: string, id?: string) {
  try {
    if (id) sessionStorage.setItem(key, id);
    else sessionStorage.removeItem(key);
  } catch {
    // The current generation is tracked in state when storage is unavailable.
  }
}

export function useStudioGeneration(campaignId: string, onSucceeded: () => void) {
  const storageKey = `iteam:creative-generation:${campaignId}`;
  const [jobId, setJobId] = useState<string>();
  const [job, setJob] = useState<StudioGenerationJob>();
  const [stopped, setStopped] = useState(false);
  const [authenticationError, setAuthenticationError] = useState<ApiError>();
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

  const clearTracking = useCallback(() => {
    setJobId(undefined);
    setJob(undefined);
    setStopped(false);
    setAuthenticationError(undefined);
    storeTrackedJob(storageKey);
  }, [storageKey]);

  const update = useCallback((next: StudioGenerationJob) => {
    setJobId(next.id);
    setJob(next);
    setStopped(false);
    setAuthenticationError(undefined);
    storeTrackedJob(storageKey, shouldStoreJobId(next) ? next.id : undefined);
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
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.code === "CREATIVE_GENERATION_NOT_FOUND") {
          clearTracking();
          return;
        }
        if (error instanceof ApiError && error.status === 401) setAuthenticationError(error);
        setStopped(true);
      }
    }

    void poll(jobId);
    return () => {
      controller.abort();
      if (timeout !== undefined) window.clearTimeout(timeout);
    };
  }, [active, campaignId, clearTracking, jobId, stopped, update]);

  return {
    job,
    active,
    label: job ? STATUS_LABEL[job.status] : jobId ? "Проверяем задачу генерации…" : undefined,
    stopped,
    authenticationError,
    update,
    resume: () => {
      setAuthenticationError(undefined);
      setStopped(false);
    },
    dismiss: () => {
      if (job?.status === "FAILED" || job?.status === "UNKNOWN") clearTracking();
    },
  };
}
