import React, { FormEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Tab = "recommend" | "record" | "wishlist" | "notInterested";

type SearchCandidate = {
  subject_id: string;
  title: string;
  year: number | null;
  director: string | null;
  url: string | null;
};

type RecommendationItem = {
  id: string;
  rank: number;
  slot_type: "exploit" | "explore";
  score: number;
  source_ref: string | null;
  source_label: string | null;
  processing_status: string | null;
  processed_at: string | null;
  movie: {
    id: string;
    title: string;
    year: number;
    director: string;
    directors: string[];
    main_cast: string[];
    cast: string[];
    douban_rating: number;
    douban_url: string;
    poster_url: string | null;
  };
};

type RecommendationSession = {
  id: string;
  strategy: string;
  created_at: string;
  debug_metadata?: Record<string, unknown>;
  items: RecommendationItem[];
};

type WishlistItem = {
  id: string;
  status: string;
  source_session_id: string;
  score: number | null;
  source_ref: string | null;
  source_label: string | null;
  created_at: string;
  closed_at: string | null;
  movie: RecommendationItem["movie"];
};

type NotInterestedItem = {
  id: string;
  movie_id: string;
  state: "not_interested";
  state_changed_at: string;
  session_id: string;
  item_id: string;
  movie: RecommendationItem["movie"];
};

type RecordForm = {
  watched_date: string;
  rating: string;
  quality: "1080p" | "4K" | "Other";
  custom_quality: string;
  comment: string;
};

type RecordHandoff = {
  movie: SearchCandidate;
  sourceTab: Tab;
  session_id?: string;
  recommendation_item_id?: string;
  wishlist_id?: string;
};

type ProcessedRecommendationItem = {
  session_id: string;
  recommendation_item_id: string;
  processing_status: string | null;
  processed_at: string | null;
};

type RecordViewingHistoryResponse = {
  session_id?: string;
  recommendation_item_id?: string;
  processing_status?: string | null;
  processed_at?: string | null;
};

const today = new Date().toISOString().slice(0, 10);
const ACTIVE_TAB_KEY = "movies.frontend.activeTab";
const RECOMMENDATION_SESSION_KEY = "movies.frontend.currentRecommendationSession";
const WISHLIST_CACHE_KEY = "movies.frontend.wishlist.firstPage";
const NOT_INTERESTED_CACHE_KEY = "movies.frontend.notInterested.firstPage";
const RECORD_DRAFT_KEY = "movies.frontend.recordWatchedDraft";
const DEBUG_MODE_KEY = "movies.frontend.debugMode";
const BASELINE_HYBRID_TOTAL = 23.4568;

function defaultRecordForm(): RecordForm {
  return {
    watched_date: today,
    rating: "4.0",
    quality: "1080p",
    custom_quality: "",
    comment: ""
  };
}

function useStoredState<T>(key: string, initialValue: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = window.localStorage.getItem(key);
      return stored ? (JSON.parse(stored) as T) : initialValue;
    } catch {
      return initialValue;
    }
  });

  useEffect(() => {
    window.localStorage.setItem(key, JSON.stringify(value));
  }, [key, value]);

  return [value, setValue] as const;
}

function App() {
  const [tab, setTab] = useStoredState<Tab>(ACTIVE_TAB_KEY, "recommend");
  const [debugMode, setDebugMode] = useStoredState<boolean>(DEBUG_MODE_KEY, false);
  const [recordHandoff, setRecordHandoff] = useState<RecordHandoff | null>(null);
  const [processedRecommendationItem, setProcessedRecommendationItem] = useState<ProcessedRecommendationItem | null>(null);
  const [wishlistRefreshKey, setWishlistRefreshKey] = useState(0);
  const [notInterestedRefreshKey, setNotInterestedRefreshKey] = useState(0);
  const loadPosters = !debugMode;

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

  return (
    <div className="app-shell">
      <header className="topbar">
        <nav className="tabs" aria-label="Main views">
          <button className={tab === "recommend" ? "active" : ""} onClick={() => switchTab("recommend")}>
            Recommend
          </button>
          <button className={tab === "record" ? "active" : ""} onClick={() => switchTab("record")}>
            Add watched
          </button>
          <button className={tab === "wishlist" ? "active" : ""} onClick={() => switchTab("wishlist")}>
            Wishlist
          </button>
          <button className={tab === "notInterested" ? "active" : ""} onClick={() => switchTab("notInterested")}>
            Not interested
          </button>
        </nav>
        <label className="debug-toggle">
          <input type="checkbox" checked={debugMode} onChange={(event) => setDebugMode(event.target.checked)} />
          Debug
        </label>
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
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function restore() {
      if (session?.id) {
        try {
          const synced = await api<RecommendationSession>(`/recommendations/${session.id}`);
          if (!cancelled) setSession(synced);
        } catch (error) {
          if (!cancelled) setStatus(errorMessage(error));
        }
        return;
      }
      await loadRecommendations(debugMode);
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

  async function loadRecommendations(nextDebugMode = debugMode) {
    setLoading(true);
    setStatus("");
    try {
      const query = recommendationQuery(nextDebugMode);
      const data = await api<RecommendationSession>(`/recommendations${query}`);
      setSession(data);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function refreshRecommendations() {
    await loadRecommendations(debugMode);
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
    if (!session) return;
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
      setStatus(errorMessage(error));
    }
  }

  return (
    <section className="panel">
      <div className="toolbar align-right">
        <button className="primary" onClick={refreshRecommendations} disabled={loading}>
          Refresh
        </button>
        <StatusText value={status} />
      </div>
      <div className="movie-grid">
        {session?.items.map((item) => (
          <SharedMovieCard
            key={item.id}
            movie={item.movie}
            badge={item.slot_type === "explore" ? "Explore" : "Exploit"}
            score={normalizedScore(item.score)}
            sourceLabel={item.source_label || item.source_ref || undefined}
            processedStatus={item.processing_status}
            loadPoster={loadPosters}
          >
            <div className="button-row">
              <button onClick={() => startWatched(item)} disabled={Boolean(item.processing_status)}>
                Watched
              </button>
              <button onClick={() => submitFeedback(item, "want_to_watch")} disabled={Boolean(item.processing_status)}>
                +
              </button>
              <button onClick={() => submitFeedback(item, "not_interested")} disabled={Boolean(item.processing_status)}>
                -
              </button>
              <button onClick={() => submitFeedback(item, "maybe_later")} disabled={Boolean(item.processing_status)}>
                Later
              </button>
            </div>
          </SharedMovieCard>
        ))}
      </div>
    </section>
  );
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
    setQuery(handoff.movie.title);
    setCandidates([handoff.movie]);
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
      <form className="panel" onSubmit={search}>
        <div className="toolbar wide">
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Enter name or id" />
          <button className="primary" disabled={loading || !query.trim()}>
            Search
          </button>
          <StatusText value={status} />
        </div>
        <div className="candidate-list">
          {candidates.map((candidate) => (
            <button
              type="button"
              className={selected?.subject_id === candidate.subject_id ? "candidate selected" : "candidate"}
              key={candidate.subject_id}
              onClick={() => setSelected(candidate)}
            >
              <span>{candidate.title}</span>
              <small>
                {[candidate.year, candidate.director].filter(Boolean).join(" · ") || candidate.subject_id}
              </small>
            </button>
          ))}
        </div>
      </form>
      <form className="panel form-grid" onSubmit={submit}>
        <h2>{selected ? selected.title : "Selected movie"}</h2>
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
          <textarea value={form.comment} onChange={(event) => setForm({ ...form, comment: event.target.value })} />
        </label>
        <button className="primary span-2" disabled={loading || !selected}>
          Save
        </button>
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
  const [items, setItems] = useStoredState<WishlistItem[]>(WISHLIST_CACHE_KEY, []);
  const [status, setStatus] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [filterText, setFilterText] = useState("");
  const visibleItems = useMemo(
    () => items.filter((item) => movieMatchesFilter(item.movie, filterText)),
    [items, filterText]
  );

  useEffect(() => {
    void loadWishlist();
  }, [refreshKey]);

  useEffect(() => {
    function onScroll() {
      const nearBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 240;
      if (nearBottom) void loadNextPage();
    }
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, [items.length, total, loading]);

  async function loadWishlist() {
    setLoading(true);
    setStatus("");
    try {
      const data = await api<{ items: WishlistItem[]; total: number }>("/wishlist?limit=10&offset=0");
      setItems(data.items);
      setTotal(data.total);
      setLoaded(true);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function loadNextPage() {
    if (loading || (total !== null && items.length >= total)) return;
    setLoading(true);
    setStatus("");
    try {
      const data = await api<{ items: WishlistItem[]; total: number }>(`/wishlist?limit=10&offset=${items.length}`);
      setItems([...items, ...data.items]);
      setTotal(data.total);
      setLoaded(true);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function removeWishlistItem(item: WishlistItem) {
    setStatus("");
    try {
      await api(`/wishlist/${item.id}`, { method: "DELETE" });
      setItems(items.filter((candidate) => candidate.id !== item.id));
      setTotal((current) => (current === null ? current : Math.max(0, current - 1)));
    } catch (error) {
      setStatus(errorMessage(error));
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
        <input value={filterText} onChange={(event) => setFilterText(event.target.value)} placeholder="Filter" />
        <StatusText value={status} />
      </div>
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
              <button className="icon-button" onClick={() => removeWishlistItem(item)} aria-label="Remove" title="Remove">
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
      {loaded && items.length === 0 && <p className="empty">No active wishlist items.</p>}
      {loaded && items.length > 0 && visibleItems.length === 0 && <p className="empty">No matching wishlist items.</p>}
    </section>
  );
}

function NotInterestedView({ refreshKey, loadPosters }: { refreshKey: number; loadPosters: boolean }) {
  const [items, setItems] = useStoredState<NotInterestedItem[]>(NOT_INTERESTED_CACHE_KEY, []);
  const [status, setStatus] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [filterText, setFilterText] = useState("");
  const visibleItems = useMemo(
    () => items.filter((item) => movieMatchesFilter(item.movie, filterText)),
    [items, filterText]
  );

  useEffect(() => {
    void loadNotInterested();
  }, [refreshKey]);

  useEffect(() => {
    function onScroll() {
      const nearBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 240;
      if (nearBottom) void loadNextPage();
    }
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, [items.length, total, loading]);

  async function loadNotInterested() {
    setLoading(true);
    setStatus("");
    try {
      const data = await api<{ items: NotInterestedItem[]; total: number }>("/not-interested?limit=10&offset=0");
      setItems(data.items);
      setTotal(data.total);
      setLoaded(true);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function loadNextPage() {
    if (loading || (total !== null && items.length >= total)) return;
    setLoading(true);
    setStatus("");
    try {
      const data = await api<{ items: NotInterestedItem[]; total: number }>(
        `/not-interested?limit=10&offset=${items.length}`
      );
      setItems([...items, ...data.items]);
      setTotal(data.total);
      setLoaded(true);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function removeNotInterested(item: NotInterestedItem) {
    setStatus("");
    try {
      await api(`/not-interested/${item.movie_id}`, { method: "DELETE" });
      setItems(items.filter((candidate) => candidate.movie_id !== item.movie_id));
      setTotal((current) => (current === null ? current : Math.max(0, current - 1)));
    } catch (error) {
      setStatus(errorMessage(error));
    }
  }

  return (
    <section className="panel">
      <div className="toolbar wide">
        <input value={filterText} onChange={(event) => setFilterText(event.target.value)} placeholder="Filter" />
        <StatusText value={status} />
      </div>
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
              <button className="icon-button" onClick={() => removeNotInterested(item)} aria-label="Remove" title="Remove">
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
      {loaded && items.length === 0 && <p className="empty">No not-interested movies.</p>}
      {loaded && items.length > 0 && visibleItems.length === 0 && <p className="empty">No matching not-interested movies.</p>}
    </section>
  );
}

function SharedMovieCard({
  movie,
  badge,
  score,
  sourceLabel,
  processedStatus,
  loadPoster = true,
  children
}: {
  movie: RecommendationItem["movie"];
  badge: string;
  score?: number;
  sourceLabel?: string;
  processedStatus?: string | null;
  loadPoster?: boolean;
  children?: React.ReactNode;
}) {
  const [posterFailed, setPosterFailed] = useState(false);
  const [posterLoaded, setPosterLoaded] = useState(false);
  const directors = useMemo(
    () => formatPersonNames(movie.directors?.length ? movie.directors : [movie.director]),
    [movie]
  );
  const cast = useMemo(() => formatPersonNames(movie.cast?.length ? movie.cast : movie.main_cast).slice(0, 3).join(", "), [movie]);
  const statusText = processedStatus ? processingStatusLabel(processedStatus) : null;
  const showPoster = loadPoster && Boolean(movie.poster_url) && !posterFailed;

  useEffect(() => {
    setPosterFailed(false);
    setPosterLoaded(false);
  }, [movie.poster_url]);

  return (
    <article className={processedStatus ? "movie-card processed" : "movie-card"}>
      <div className="card-badge-row">
        <span>{badge}</span>
        <span>Douban {movie.douban_rating.toFixed(1)}</span>
      </div>
      {showPoster && movie.poster_url ? (
        <img
          className={posterLoaded ? "poster-image loaded" : "poster-image loading"}
          src={movie.poster_url}
          alt=""
          loading="lazy"
          onLoad={() => setPosterLoaded(true)}
          onError={() => {
            setPosterLoaded(false);
            setPosterFailed(true);
          }}
        />
      ) : (
        <div className={posterFailed ? "poster-placeholder failed" : "poster-placeholder"} aria-hidden="true" />
      )}
      <h3>
        <a href={movie.douban_url} target="_blank" rel="noreferrer">
          {movie.title}
        </a>
      </h3>
      <p>{[movie.year || null, directors.join(", ")].filter(Boolean).join(" | ")}</p>
      <p>{cast}</p>
      <div className="score-source-row">
        <span>{score === undefined ? "" : `Score: ${score}`}</span>
        <span>{sourceLabel || ""}</span>
      </div>
      {statusText && <p className="processed-label">{statusText}</p>}
      {children}
    </article>
  );
}
function StatusText({ value }: { value: string }) {
  return value ? <span className="status">{value}</span> : null;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  await window.moviesDesktop?.waitForBackend();
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {})
    }
  });
  const contentType = response.headers.get("content-type") || "";
  if (!response.ok) {
    const body = contentType.includes("application/json") ? await response.json().catch(() => ({})) : {};
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  if (!contentType.includes("application/json")) {
    throw new Error(`Expected JSON from ${path}, got ${contentType || "unknown content type"}`);
  }
  return response.json() as Promise<T>;
}

function apiUrl(path: string) {
  if (/^https?:\/\//.test(path)) return path;
  const baseUrl = window.moviesDesktop?.apiBaseUrl || import.meta.env.VITE_API_BASE_URL || "";
  return baseUrl ? `${baseUrl}${path}` : path;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Request failed";
}

function normalizedScore(score: number) {
  return Math.round((score / BASELINE_HYBRID_TOTAL) * 100);
}

function recommendationQuery(debugMode: boolean) {
  const params = new URLSearchParams({ strategy: "hybrid" });
  if (debugMode) {
    params.set("exposure_cooldown_sessions", "1");
    params.set("seed", "42");
  }
  return `?${params.toString()}`;
}

function processingStatusLabel(status: string) {
  return (
    {
      watched: "Watched",
      added_to_wishlist: "Added to wishlist",
      not_interested: "Not interested",
      maybe_later: "Maybe later"
    }[status] || status
  );
}

function movieMatchesFilter(movie: RecommendationItem["movie"], filterText: string) {
  const query = filterText.trim().toLocaleLowerCase();
  if (!query) return true;
  return movieFilterText(movie).includes(query);
}

function movieFilterText(movie: RecommendationItem["movie"]) {
  return [
    movie.title,
    movie.year,
    movie.director,
    ...(movie.directors || []),
    ...(movie.main_cast || []),
    ...(movie.cast || [])
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase();
}

function formatPersonNames(values: Array<string | null | undefined>) {
  return values.map((value) => formatPersonName(value)).filter(Boolean);
}

function formatPersonName(value: string | null | undefined) {
  const text = (value || "").trim();
  const match = text.match(/[a-zA-Z]/);
  if (!match || match.index === undefined) return text;

  const localPart = text.slice(0, match.index).trim();
  const foreignPart = text.slice(match.index).trim();
  if (!localPart) return foreignPart;
  if (hasMiddleDot(localPart)) return foreignPart || localPart;
  if (!/[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]/.test(localPart)) return foreignPart || localPart;
  return localPart;
}

function hasMiddleDot(value: string) {
  return ["·", "・", "•", ".", "┞"].some((marker) => value.includes(marker));
}

function TrashIcon() {
  return (
    <svg className="button-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v5" />
      <path d="M14 11v5" />
    </svg>
  );
}

function searchCandidateFromMovie(movie: RecommendationItem["movie"]): SearchCandidate {
  return {
    subject_id: subjectIdFromDoubanUrl(movie.douban_url) || movie.id,
    title: movie.title,
    year: movie.year,
    director: movie.director,
    url: movie.douban_url
  };
}

function subjectIdFromDoubanUrl(url: string) {
  return url.match(/\/subject\/([^/]+)\//)?.[1] || null;
}

function selectedQuality(form: RecordForm) {
  return form.quality === "Other" ? form.custom_quality.trim() : form.quality;
}

function sheetFromWatchedDate(watchedDate: string) {
  return watchedDate.slice(0, 4);
}

createRoot(document.getElementById("root")!).render(<App />);



