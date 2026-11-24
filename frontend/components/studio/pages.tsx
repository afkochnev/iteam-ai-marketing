"use client";

import { useCallback, useEffect, useState } from "react";
import type { StudioPage } from "@/lib/studio";

interface PageQuery {
  offset: number;
}
type PageReader<T> = (offset: number, signal: AbortSignal) => Promise<StudioPage<T>>;

export function useStudioPage<T>(read: PageReader<T>, refreshCount = 0) {
  const [query, setQuery] = useState<PageQuery>({ offset: 0 });
  const [result, setResult] = useState<{
    query: PageQuery;
    read: PageReader<T>;
    refreshCount: number;
    page?: StudioPage<T>;
    error?: unknown;
  }>();

  useEffect(() => {
    const controller = new AbortController();
    void read(query.offset, controller.signal).then(
      (page) => {
        if (!controller.signal.aborted) setResult({ query, read, refreshCount, page });
      },
      (error: unknown) => {
        if (!controller.signal.aborted) setResult({ query, read, refreshCount, error });
      },
    );
    return () => controller.abort();
  }, [query, read, refreshCount]);

  const current = result?.query === query && result.read === read && result.refreshCount === refreshCount ? result : undefined;
  const setOffset = useCallback((offset: number) => setQuery({ offset }), []);
  const refresh = useCallback((firstPage = false) => setQuery((previous) => ({ offset: firstPage ? 0 : previous.offset })), []);
  return {
    page: current?.page,
    error: current?.error,
    loading: current === undefined,
    setOffset,
    refresh,
  };
}

export function StudioPagination({
  page,
  noun,
  onChange,
}: {
  page: StudioPage<unknown>;
  noun: string;
  onChange: (offset: number) => void;
}) {
  const { limit, offset, total } = page;
  if (offset === 0 && total <= limit) return null;
  return (
    <nav className="actions" aria-label={`Страницы · ${noun}`}>
      <button type="button" className="secondary" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        Назад
      </button>
      <span>Страница {Math.floor(offset / limit) + 1} из {Math.max(1, Math.ceil(total / limit))}</span>
      <button type="button" className="secondary" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
        Далее
      </button>
    </nav>
  );
}
