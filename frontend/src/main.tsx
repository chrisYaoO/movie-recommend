import { FormEvent, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { api, errorMessage, isErrorStatus } from "./api";
import { GearIcon, MoonIcon, RefreshIcon, SearchIcon, SunIcon, TrashIcon } from "./components/Icons";
import { SharedMovieCard } from "./components/SharedMovieCard";
import { StatusBanner, StatusText } from "./components/Status";
import {
  ACTIVE_TAB_KEY,
  DEBUG_MODE_KEY,
  NOT_INTERESTED_CACHE_KEY,
  RECOMMENDATION_SESSION_KEY,
  RECOMMENDATION_STRATEGY_KEY,
  RECORD_DRAFT_KEY,
  THEME_MODE_KEY,
  WISHLIST_CACHE_KEY,
  today
} from "./constants";
import { usePagedMovieList } from "./hooks/usePagedMovieList";
import { useStoredState } from "./hooks/useStoredState";
import type {
  NotInterestedItem,
  ProcessedRecommendationItem,
  RecommendationItem,
  RecommendationSession,
  RecommendationStrategy,
  RecordForm,
  RecordHandoff,
  RecordViewingHistoryResponse,
  SearchCandidate,
  Tab,
  ThemeMode,
  UndoRecommendationProcessingResponse,
  WishlistItem
} from "./types";
import { nextThemeMode, themeLabel } from "./utils/theme";
import { normalizedScore, recommendationReason, searchCandidateFromMovie } from "./utils/movie";

function defaultRecordForm(): RecordForm {
  return {
    watched_date: today,
    rating: "4.0",
    quality: "1080p",
    custom_quality: "",
    comment: ""
  };
}

function App() {
  const [tab, setTab] = useStoredState<Tab>(ACTIVE_TAB_KEY, "recommend");
  const [debugMode, setDebugMode] = useStoredState<boolean>(DEBUG_MODE_KEY, false);
  const [themeMode, setThemeMode] = useStoredState<ThemeMode>(THEME_MODE_KEY, "system");
  const [recordHandoff, setRecordHandoff] = useState<RecordHandoff | null>(null);
  const [processedRecommendationItem, setProcessedRecommendationItem] = useState<ProcessedRecommendationItem | null>(null);
  const [wishlistRefreshKey, setWishlistRefreshKey] = useState(0);
  const [notInterestedRefreshKey, setNotInterestedRefreshKey] = useState(0);
  const loadPosters = !debugMode;

  useEffect(() => {
    if (themeMode === "system") {
      document.documentElement.removeAttribute("data-theme");
      return;
    }
    document.documentElement.setAttribute("data-theme", themeMode);
  }, [themeMode]);

  function switchTab(nextTab: Tab) {
    if (nextTab === tab) return;
    setTab(nextTab);
    if (nextTab === "wishlist") setWishlistRefreshKey((current) => current + 1);
    if (nextTab === "notInterested") setNotInterestedRefreshKey((current) => current + 1);
  }

  function openRecordWatched(handoff: RecordHandoff) {
    setRecordHandoff(handoff);
    switchTab("record");
  }

  function cycleTheme() {
    setThemeMode(nextThemeMode(themeMode));
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <nav className="tabs" aria-label="Main views">
          <button
            className={tab === "recommend" ? "active" : ""}
            onClick={() => switchTab("recommend")}
            aria-current={tab === "recommend" ? "page" : undefined}
          >
            Recommend
          </button>
          <button
            className={tab === "record" ? "active" : ""}
            onClick={() => switchTab("record")}
            aria-current={tab === "record" ? "page" : undefined}
          >
            Add watched
          </button>
          <button
            className={tab === "wishlist" ? "active" : ""}
            onClick={() => switchTab("wishlist")}
            aria-current={tab === "wishlist" ? "page" : undefined}
          >
            Wishlist
          </button>
          <button
            className={tab === "notInterested" ? "active" : ""}
            onClick={() => switchTab("notInterested")}
            aria-current={tab === "notInterested" ? "page" : undefined}
          >
            Not interested
          </button>
        </nav>
        <div className="topbar-controls">
          <button
            type="button"
            className="theme-current-button"
            onClick={cycleTheme}
            aria-label={`Current theme: ${themeLabel(themeMode)}. Click to switch to ${themeLabel(nextThemeMode(themeMode))}.`}
            title={`Theme: ${themeLabel(themeMode)}`}
          >
            <ThemeIcon mode={themeMode} />
          </button>
          <details className="debug-menu">
            <summary>More</summary>
            <label className="debug-toggle">
              <input type="checkbox" checked={debugMode} onChange={(event) => setDebugMode(event.target.checked)} />
              Debug mode
            </label>
          </details>
        </div>
      </header>
      <main>
        <div hidden={tab !== "recommend"}>
          <RecommendationView
            debugMode={debugMode}
            loadPosters={loadPosters}
            onRecordWatched={openRecordWatched}
            processedItem={processedRecommendationItem}
          />
        </div>
        <div hidden={tab !== "record"}>
          <RecordWatchedView
            handoff={recordHandoff}
            onRecommendationItemProcessed={setProcessedRecommendationItem}
            onCompleted={(sourceTab) => {
              setRecordHandoff(null);
              if (sourceTab && sourceTab !== "record") switchTab(sourceTab);
            }}
          />
        </div>
        <div hidden={tab !== "wishlist"}>
          <WishlistView onRecordWatched={openRecordWatched} refreshKey={wishlistRefreshKey} loadPosters={loadPosters} />
        </div>
        <div hidden={tab !== "notInterested"}>
          <NotInterestedView refreshKey={notInterestedRefreshKey} loadPosters={loadPosters} />
        </div>
      </main>
    </div>
  );
}

function RecommendationView({
  debugMode,
  loadPosters,
  onRecordWatched,
  processedItem
}: {
  debugMode: boolean;
  loadPosters: boolean;
  onRecordWatched: (handoff: RecordHandoff) => void;
  processedItem: ProcessedRecommendationItem | null;
}) {
  const [session, setSession] = useStoredState<RecommendationSession | null>(RECOMMENDATION_SESSION_KEY, null);
  const [strategy, setStrategy] = useStoredState<RecommendationStrategy>(RECOMMENDATION_STRATEGY_KEY, "hybrid");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [pendingItemIds, setPendingItemIds] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    let cancelled = false;
    async function restore() {
      if (session?.id) {
        try {
          const synced = await api<RecommendationSession>(`/recommendations/${session.id}`);
          if (!cancelled) setSession(synced);
        } catch (error) {
          if (!cancelled) setStatus(`Could not sync recommendation session: ${errorMessage(error)}. Showing cached results.`);
        }
        return;
      }
      await loadRecommendations(debugMode, strategy);
    }
    void restore();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!processedItem?.processing_status) return;
    setSession((current) => {
      if (!current || current.id !== processedItem.session_id) return current;
      let changed = false;
      const items = current.items.map((item) => {
        if (item.id !== processedItem.recommendation_item_id) return item;
        if (item.processing_status === processedItem.processing_status && item.processed_at === processedItem.processed_at) {
          return item;
        }
        changed = true;
        return {
          ...item,
          processing_status: processedItem.processing_status,
          processed_at: processedItem.processed_at || new Date().toISOString()
        };
      });
      return changed ? { ...current, items } : current;
    });
  }, [processedItem, setSession]);

  async function loadRecommendations(nextDebugMode = debugMode, nextStrategy = strategy) {
    setLoading(true);
    setStatus("");
    try {
      const query = recommendationQuery(nextDebugMode, nextStrategy);
      const data = await api<RecommendationSession>(`/recommendations${query}`);
      setSession(data);
    } catch (error) {
      setStatus(`Could not refresh recommendations: ${errorMessage(error)}. Showing cached results.`);
    } finally {
      setLoading(false);
    }
  }

  async function refreshRecommendations() {
    await loadRecommendations(debugMode, strategy);
  }

  async function changeStrategy(nextStrategy: RecommendationStrategy) {
    setStrategy(nextStrategy);
    await loadRecommendations(debugMode, nextStrategy);
  }

  function startWatched(item: RecommendationItem) {
    onRecordWatched({
      movie: searchCandidateFromMovie(item.movie),
      sourceTab: "recommend",
      session_id: session?.id,
      recommendation_item_id: item.id
    });
  }

  async function submitFeedback(item: RecommendationItem, feedbackType: string) {
    if (!session || pendingItemIds.has(item.id)) return;
    setPendingItem(item.id, true);
    setStatus("");
    try {
      await api(`/recommendations/${session.id}/items/${item.id}/feedback`, {
        method: "POST",
        body: JSON.stringify({ feedback_type: feedbackType })
      });
      const processingStatusByFeedback: Record<string, string> = {
        want_to_watch: "added_to_wishlist",
        maybe_later: "maybe_later",
        not_interested: "not_interested"
      };
      const processingStatus = processingStatusByFeedback[feedbackType] || null;
      if (processingStatus) {
        setSession({
          ...session,
          items: session.items.map((candidate) =>
            candidate.id === item.id
              ? { ...candidate, processing_status: processingStatus, processed_at: new Date().toISOString() }
              : candidate
          )
        });
      }
      setStatus("Saved");
    } catch (error) {
      setStatus(`Could not save feedback: ${errorMessage(error)}`);
    } finally {
      setPendingItem(item.id, false);
    }
  }

  return (
    <section className="panel">
      <div className="toolbar recommendation-toolbar">
        <div className="recommendation-controls">
          <label className="strategy-control">
            Strategy
            <select
              value={strategy}
              onChange={(event) => void changeStrategy(event.target.value as RecommendationStrategy)}
              disabled={loading}
            >
              <option value="hybrid">Hybrid</option>
              <option value="bandit_hybrid">Bandit hybrid</option>
            </select>
          </label>
          <button
            className="primary icon-button"
            onClick={refreshRecommendations}
            disabled={loading}
            aria-label="Refresh recommendations"
            title="Refresh"
          >
            <RefreshIcon />
          </button>
        </div>
        <StatusBanner value={status} onRetry={isErrorStatus(status) ? refreshRecommendations : undefined} />
      </div>
      <div className="movie-grid">
        {session?.items.map((item) => (
          <SharedMovieCard
            key={item.id}
            movie={item.movie}
            badge={item.slot_type === "explore" ? "Explore" : "Exploit"}
            score={normalizedScore(item.score)}
            sourceLabel={item.source_label || item.source_ref || undefined}
            why={recommendationReason(item)}
            processedStatus={item.processing_status}
            loadPoster={loadPosters}
            onUndoProcessed={item.processing_status && !pendingItemIds.has(item.id) ? () => undoProcessing(item) : undefined}
          >
            <div className="button-row">
              <button onClick={() => startWatched(item)} disabled={isRecommendationActionDisabled(item)}>
                Watched
              </button>
              <button
                onClick={() => submitFeedback(item, "want_to_watch")}
                disabled={isRecommendationActionDisabled(item)}
                aria-label="Add to wishlist"
                title="Add to wishlist"
              >
                +
              </button>
              <button
                onClick={() => submitFeedback(item, "not_interested")}
                disabled={isRecommendationActionDisabled(item)}
                aria-label="Not interested"
                title="Not interested"
              >
                -
              </button>
              <button onClick={() => submitFeedback(item, "maybe_later")} disabled={isRecommendationActionDisabled(item)}>
                Later
              </button>
            </div>
          </SharedMovieCard>
        ))}
      </div>
    </section>
  );

  async function undoProcessing(item: RecommendationItem) {
    if (!session || pendingItemIds.has(item.id)) return;
    setPendingItem(item.id, true);
    setStatus("");
    try {
      const updated = await api<UndoRecommendationProcessingResponse>(
        `/recommendations/${session.id}/items/${item.id}/processing`,
        { method: "DELETE" }
      );
      setSession({
        ...session,
        items: session.items.map((candidate) =>
          candidate.id === item.id
            ? { ...candidate, processing_status: updated.processing_status, processed_at: updated.processed_at }
            : candidate
        )
      });
      setStatus("Undone");
    } catch (error) {
      setStatus(`Could not undo: ${errorMessage(error)}`);
    } finally {
      setPendingItem(item.id, false);
    }
  }

  function isRecommendationActionDisabled(item: RecommendationItem) {
    return Boolean(item.processing_status) || pendingItemIds.has(item.id);
  }

  function setPendingItem(itemId: string, pending: boolean) {
    setPendingItemIds((current) => {
      const next = new Set(current);
      if (pending) next.add(itemId);
      else next.delete(itemId);
      return next;
    });
  }
}

function RecordWatchedView({
  handoff,
  onRecommendationItemProcessed,
  onCompleted
}: {
  handoff: RecordHandoff | null;
  onRecommendationItemProcessed: (item: ProcessedRecommendationItem) => void;
  onCompleted: (sourceTab: Tab | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<SearchCandidate[]>([]);
  const [selected, setSelected] = useStoredState<SearchCandidate | null>(`${RECORD_DRAFT_KEY}.selected`, null);
  const [form, setForm] = useStoredState<RecordForm>(`${RECORD_DRAFT_KEY}.form`, defaultRecordForm());
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!handoff) return;
    setSelected(handoff.movie);
    setQuery("");
    setCandidates([]);
  }, [handoff]);

  async function search(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setStatus("");
    try {
      const data = await api<{ items: SearchCandidate[] }>(`/movies/search?q=${encodeURIComponent(query)}`);
      setCandidates(data.items);
      if (data.items.length === 0) setStatus("No results");
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!selected) {
      setStatus("Select a movie");
      return;
    }
    setLoading(true);
    setStatus("");
    try {
      const response = await api<RecordViewingHistoryResponse>("/viewing-history", {
        method: "POST",
        body: JSON.stringify({
          douban_subject_id: selected.subject_id,
          watched_date: form.watched_date,
          rating: Number(form.rating),
          quality: selectedQuality(form) || null,
          comment: form.comment || null,
          sheet: sheetFromWatchedDate(form.watched_date),
          session_id: handoff?.session_id || null,
          recommendation_item_id: handoff?.recommendation_item_id || null,
          wishlist_id: handoff?.wishlist_id || null
        })
      });
      if (response.session_id && response.recommendation_item_id) {
        onRecommendationItemProcessed({
          session_id: response.session_id,
          recommendation_item_id: response.recommendation_item_id,
          processing_status: response.processing_status || null,
          processed_at: response.processed_at || null
        });
      }
      setStatus("Recorded");
      setCandidates([]);
      setSelected(null);
      setQuery("");
      setForm(defaultRecordForm());
      window.localStorage.removeItem(`${RECORD_DRAFT_KEY}.selected`);
      window.localStorage.removeItem(`${RECORD_DRAFT_KEY}.form`);
      onCompleted(handoff?.sourceTab || null);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="record-layout">
      <form className="panel record-search-panel" onSubmit={search}>
        <div className="record-search-row">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search Douban by title or subject id"
          />
          <button className="primary icon-button" disabled={loading || !query.trim()} aria-label="Search" title="Search">
            <SearchIcon />
          </button>
        </div>
        <StatusText value={status} />
        <div className="candidate-list">
          {candidates.map((candidate) => (
            <button
              type="button"
              className={selected?.subject_id === candidate.subject_id ? "candidate selected" : "candidate"}
              key={candidate.subject_id}
              onClick={() => setSelected(candidate)}
            >
              <span>{candidate.title}</span>
              <small>{recordCandidateMeta(candidate)}</small>
            </button>
          ))}
        </div>
      </form>
      <form className="panel record-form-panel" onSubmit={submit}>
        <div className="record-section-header">
          <div>
            <h2>{selected ? selected.title : "Review"}</h2>
            {selected && <p>{recordCandidateMeta(selected)}</p>}
          </div>
        </div>
        <div className="form-grid">
          <label>
            Date
            <input
              type="date"
              value={form.watched_date}
              onChange={(event) => setForm({ ...form, watched_date: event.target.value })}
            />
          </label>
          <label>
            Rating
            <input
              type="number"
              min="0"
              max="5"
              step="0.1"
              value={form.rating}
              onChange={(event) => setForm({ ...form, rating: event.target.value })}
            />
          </label>
          <label>
            Quality
            <select value={form.quality} onChange={(event) => setForm({ ...form, quality: event.target.value as RecordForm["quality"] })}>
              <option value="1080p">1080p</option>
              <option value="4K">4K</option>
              <option value="Other">Other</option>
            </select>
          </label>
          {form.quality === "Other" && (
            <label>
              Custom quality
              <input value={form.custom_quality} onChange={(event) => setForm({ ...form, custom_quality: event.target.value })} />
            </label>
          )}
          <label className="span-2">
            Comment
            <textarea
              value={form.comment}
              onChange={(event) => setForm({ ...form, comment: event.target.value })}
              placeholder="Optional notes"
            />
          </label>
        </div>
        <div className="form-actions">
          <button className="primary" disabled={loading || !selected}>
            Save
          </button>
        </div>
      </form>
    </section>
  );
}

function WishlistView({
  onRecordWatched,
  refreshKey,
  loadPosters
}: {
  onRecordWatched: (handoff: RecordHandoff) => void;
  refreshKey: number;
  loadPosters: boolean;
}) {
  const {
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
  } = usePagedMovieList<WishlistItem>({
    cacheKey: WISHLIST_CACHE_KEY,
    endpoint: "/wishlist",
    refreshKey,
    loadError: "Could not load wishlist",
    loadMoreError: "Could not load more wishlist items"
  });
  const [pendingItemIds, setPendingItemIds] = useState<Set<string>>(() => new Set());

  async function removeWishlistItem(item: WishlistItem) {
    if (pendingItemIds.has(item.id)) return;
    setPendingItem(item.id, true);
    setStatus("");
    try {
      await api(`/wishlist/${item.id}`, { method: "DELETE" });
      setItems((current) => current.filter((candidate) => candidate.id !== item.id));
      setTotal((current) => (current === null ? current : Math.max(0, current - 1)));
    } catch (error) {
      setStatus(`Could not remove wishlist item: ${errorMessage(error)}`);
    } finally {
      setPendingItem(item.id, false);
    }
  }

  function startWatched(item: WishlistItem) {
    onRecordWatched({
      movie: searchCandidateFromMovie(item.movie),
      sourceTab: "wishlist",
      wishlist_id: item.id
    });
  }

  return (
    <section className="panel">
      <div className="toolbar wide">
        <input value={filterText} onChange={(event) => setFilterText(event.target.value)} placeholder="Search" />
        <StatusBanner value={status} onRetry={isErrorStatus(status) ? () => void loadFirstPage() : undefined} />
      </div>
      {!loaded && loading && <LoadingMovieGrid />}
      <div className="movie-grid">
        {visibleItems.map((item) => (
          <SharedMovieCard
            key={item.id}
            movie={item.movie}
            badge="Wishlist"
            score={item.score === null ? undefined : normalizedScore(item.score)}
            sourceLabel={item.source_label || item.source_ref || undefined}
            loadPoster={loadPosters}
          >
            <div className="button-row">
              <button onClick={() => startWatched(item)}>Watched</button>
              <button
                className="icon-button"
                onClick={() => removeWishlistItem(item)}
                disabled={pendingItemIds.has(item.id)}
                aria-label="Remove"
                title="Remove"
              >
                <TrashIcon />
              </button>
            </div>
          </SharedMovieCard>
        ))}
      </div>
      {loaded && total !== null && items.length < total && (
        <button className="load-more" onClick={loadNextPage} disabled={loading}>
          {loading ? "Loading" : "Load more"}
        </button>
      )}
      <div ref={sentinelRef} aria-hidden="true" />
      {loaded && items.length === 0 && (
        <EmptyState
          title="No active wishlist items"
          description="Movies you add from recommendations will show up here until you watch or remove them."
          actionLabel="Refresh"
          onAction={() => void loadFirstPage()}
        />
      )}
      {loaded && items.length > 0 && visibleItems.length === 0 && (
        <EmptyState
          title="No matching wishlist items"
          description="Try a different title, year, director, or cast name."
          actionLabel="Clear filter"
          onAction={() => setFilterText("")}
        />
      )}
    </section>
  );

  function setPendingItem(itemId: string, pending: boolean) {
    setPendingItemIds((current) => {
      const next = new Set(current);
      if (pending) next.add(itemId);
      else next.delete(itemId);
      return next;
    });
  }
}

function NotInterestedView({ refreshKey, loadPosters }: { refreshKey: number; loadPosters: boolean }) {
  const {
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
  } = usePagedMovieList<NotInterestedItem>({
    cacheKey: NOT_INTERESTED_CACHE_KEY,
    endpoint: "/not-interested",
    refreshKey,
    loadError: "Could not load not-interested movies",
    loadMoreError: "Could not load more not-interested movies"
  });
  const [pendingItemIds, setPendingItemIds] = useState<Set<string>>(() => new Set());

  async function removeNotInterested(item: NotInterestedItem) {
    if (pendingItemIds.has(item.movie_id)) return;
    setPendingItem(item.movie_id, true);
    setStatus("");
    try {
      await api(`/not-interested/${item.movie_id}`, { method: "DELETE" });
      setItems((current) => current.filter((candidate) => candidate.movie_id !== item.movie_id));
      setTotal((current) => (current === null ? current : Math.max(0, current - 1)));
    } catch (error) {
      setStatus(`Could not remove not-interested movie: ${errorMessage(error)}`);
    } finally {
      setPendingItem(item.movie_id, false);
    }
  }

  return (
    <section className="panel">
      <div className="toolbar wide">
        <input value={filterText} onChange={(event) => setFilterText(event.target.value)} placeholder="Search" />
        <StatusBanner value={status} onRetry={isErrorStatus(status) ? () => void loadFirstPage() : undefined} />
      </div>
      {!loaded && loading && <LoadingMovieGrid />}
      <div className="movie-grid">
        {visibleItems.map((item) => (
          <SharedMovieCard
            key={item.id}
            movie={item.movie}
            badge="Not interested"
            processedStatus="not_interested"
            loadPoster={loadPosters}
          >
            <div className="button-row">
              <button
                className="icon-button"
                onClick={() => removeNotInterested(item)}
                disabled={pendingItemIds.has(item.movie_id)}
                aria-label="Remove"
                title="Remove"
              >
                <TrashIcon />
              </button>
            </div>
          </SharedMovieCard>
        ))}
      </div>
      {loaded && total !== null && items.length < total && (
        <button className="load-more" onClick={loadNextPage} disabled={loading}>
          {loading ? "Loading" : "Load more"}
        </button>
      )}
      <div ref={sentinelRef} aria-hidden="true" />
      {loaded && items.length === 0 && (
        <EmptyState
          title="No not-interested movies"
          description="Movies you dismiss from recommendations will be collected here."
          actionLabel="Refresh"
          onAction={() => void loadFirstPage()}
        />
      )}
      {loaded && items.length > 0 && visibleItems.length === 0 && (
        <EmptyState
          title="No matching not-interested movies"
          description="Try a different title, year, director, or cast name."
          actionLabel="Clear filter"
          onAction={() => setFilterText("")}
        />
      )}
    </section>
  );

  function setPendingItem(itemId: string, pending: boolean) {
    setPendingItemIds((current) => {
      const next = new Set(current);
      if (pending) next.add(itemId);
      else next.delete(itemId);
      return next;
    });
  }
}

function recommendationQuery(debugMode: boolean, strategy: RecommendationStrategy) {
  const params = new URLSearchParams({ strategy });
  if (debugMode) {
    params.set("exposure_cooldown_sessions", "1");
    params.set("seed", "42");
  }
  return `?${params.toString()}`;
}

function ThemeIcon({ mode }: { mode: ThemeMode }) {
  if (mode === "light") return <SunIcon />;
  if (mode === "dark") return <MoonIcon />;
  return <GearIcon />;
}

function LoadingMovieGrid() {
  return (
    <div className="movie-grid loading-grid" aria-label="Loading movies">
      {Array.from({ length: 6 }).map((_, index) => (
        <div className="movie-card skeleton-card" key={index} aria-hidden="true">
          <div className="skeleton-line short" />
          <div className="skeleton-poster" />
          <div className="skeleton-line title" />
          <div className="skeleton-line" />
          <div className="skeleton-line medium" />
        </div>
      ))}
    </div>
  );
}

function EmptyState({
  title,
  description,
  actionLabel,
  onAction
}: {
  title: string;
  description: string;
  actionLabel: string;
  onAction: () => void;
}) {
  return (
    <div className="empty-state">
      <h2>{title}</h2>
      <p>{description}</p>
      <button type="button" onClick={onAction}>
        {actionLabel}
      </button>
    </div>
  );
}

function selectedQuality(form: RecordForm) {
  return form.quality === "Other" ? form.custom_quality.trim() : form.quality;
}

function recordCandidateMeta(candidate: SearchCandidate) {
  return [candidate.year, candidate.director].filter(Boolean).join(" · ") || candidate.subject_id;
}

function sheetFromWatchedDate(watchedDate: string) {
  return watchedDate.slice(0, 4);
}

createRoot(document.getElementById("root")!).render(<App />);



