import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, errorMessage } from "../api";
import type { Movie, PagedResponse } from "../types";
import { movieMatchesFilter } from "../utils/movie";
import { useStoredState } from "./useStoredState";

const DEFAULT_PAGE_SIZE = 10;

export function usePagedMovieList<TItem extends { movie: Movie }>({
  cacheKey,
  endpoint,
  refreshKey,
  loadError,
  loadMoreError,
  pageSize = DEFAULT_PAGE_SIZE
}: {
  cacheKey: string;
  endpoint: string;
  refreshKey: number;
  loadError: string;
  loadMoreError: string;
  pageSize?: number;
}) {
  const [items, setItems] = useStoredState<TItem[]>(cacheKey, []);
  const [status, setStatus] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [filterText, setFilterText] = useState("");
  const loadingRef = useRef(false);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  const visibleItems = useMemo(
    () => items.filter((item) => movieMatchesFilter(item.movie, filterText)),
    [items, filterText]
  );

  const requestPage = useCallback(
    (offset: number, signal?: AbortSignal) =>
      api<PagedResponse<TItem>>(`${endpoint}?limit=${pageSize}&offset=${offset}`, { signal }),
    [endpoint, pageSize]
  );

  const loadFirstPage = useCallback(
    async (signal?: AbortSignal) => {
      loadingRef.current = true;
      setLoading(true);
      setStatus("");
      try {
        const data = await requestPage(0, signal);
        setItems(data.items);
        setTotal(data.total);
        setLoaded(true);
      } catch (error) {
        if (!isAbortError(error)) {
          setStatus(`${loadError}: ${errorMessage(error)}. Showing cached results.`);
        }
      } finally {
        if (!signal?.aborted) {
          loadingRef.current = false;
          setLoading(false);
        }
      }
    },
    [loadError, requestPage, setItems]
  );

  const loadNextPage = useCallback(async () => {
    if (loadingRef.current || (total !== null && items.length >= total)) return;
    loadingRef.current = true;
    setLoading(true);
    setStatus("");
    try {
      const data = await requestPage(items.length);
      setItems((current) => [...current, ...data.items]);
      setTotal(data.total);
      setLoaded(true);
    } catch (error) {
      if (!isAbortError(error)) {
        setStatus(`${loadMoreError}: ${errorMessage(error)}`);
      }
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, [items.length, loadMoreError, requestPage, setItems, total]);

  useEffect(() => {
    const controller = new AbortController();
    void loadFirstPage(controller.signal);
    return () => controller.abort();
  }, [loadFirstPage, refreshKey]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) void loadNextPage();
      },
      { rootMargin: "240px" }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [loadNextPage]);

  return {
    items,
    setItems,
    status,
    setStatus,
    loaded,
    total,
    setTotal,
    loading,
    filterText,
    setFilterText,
    visibleItems,
    loadFirstPage,
    loadNextPage,
    sentinelRef
  };
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}
