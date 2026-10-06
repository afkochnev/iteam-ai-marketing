"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { agentRunsApi, tasksApi, type AgentRun, type Task } from "@/lib/api";

export function AutonomousPostProgress({ taskId, onComplete }: { taskId: string; onComplete: () => void }) {
  const [task, setTask] = useState<Task | null>(null);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [error, setError] = useState("");
  const notified = useRef(false);
  const finished = task?.status === "COMPLETED" || task?.status === "CANCELLED" || task?.status === "FAILED" || run?.status === "COMPLETED" || run?.status === "FAILED" || run?.status === "CANCELLED";
  useEffect(() => {
    if (finished) return;
    let disposed = false;
    let pending = false;
    const poll = async () => {
      if (pending) return;
      pending = true;
      try {
        const [value, runs] = await Promise.all([tasksApi.get(taskId), agentRunsApi.list({ task_id: taskId })]);
        if (disposed) return;
        setTask(value);
        setRun([...runs].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0] ?? null);
        setError("");
      } catch { if (!disposed) setError("Не удалось обновить состояние. Повторяем проверку; можно открыть задачу."); }
      finally { pending = false; }
    };
    const first = window.setTimeout(() => void poll(), 0);
    const timer = window.setInterval(() => void poll(), 3000);
    return () => { disposed = true; window.clearTimeout(first); window.clearInterval(timer); };
  }, [taskId, finished]);
  useEffect(() => {
    if ((run?.status === "COMPLETED" || task?.status === "COMPLETED") && !notified.current) {
      notified.current = true;
      onComplete();
    }
  }, [run?.status, task?.status, onComplete]);
  const contentId = typeof task?.output_data?.content_item_id === "string" ? task.output_data.content_item_id : null;
  const state = run?.status ?? task?.status ?? "READY";
  const label = state === "QUEUED" ? "Пост в очереди автоматического выполнения." : ["RUNNING", "IN_PROGRESS"].includes(state) ? "Создаётся пост…" : state === "COMPLETED" ? "Пост создан. Проверьте материал." : state === "FAILED" ? "Создание поста остановилось. Откройте задачу для разбора." : state === "CANCELLED" ? "Создание поста отменено." : "Задача создана и ожидает автоматического запуска";
  return <div aria-live="polite"><p role="status">{label} <Link href={`/tasks/${taskId}`}>Открыть задачу</Link></p>{error && <p role="alert">{error}</p>}{contentId && <Link href={`/content/${contentId}`}>Проверить пост</Link>}</div>;
}
