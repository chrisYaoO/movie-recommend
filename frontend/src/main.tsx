import React, { FormEvent, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Tab = "recommend" | "record" | "wishlist";

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
  movie: {
    id: string;
    title: string;
    year: number;
    director: string;
    main_cast: string[];
    douban_rating: number;
    douban_url: string;
  };
};

type RecommendationSession = {
  id: string;
  strategy: string;
  items: RecommendationItem[];
};

type WishlistItem = {
  id: string;
  status: string;
  movie: RecommendationItem["movie"];
};

type RecordForm = {
  watched_date: string;
  rating: string;
  quality: string;
  comment: string;
  sheet: string;
};

const today = new Date().toISOString().slice(0, 10);

function App() {
  const [tab, setTab] = useState<Tab>("recommend");

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <h1>Movie Recommender</h1>
          <p>Local recommendations and viewing history.</p>
        </div>
        <nav className="tabs" aria-label="Main views">
          <button className={tab === "recommend" ? "active" : ""} onClick={() => setTab("recommend")}>
            Recommend
          </button>
          <button className={tab === "record" ? "active" : ""} onClick={() => setTab("record")}>
            Add watched
          </button>
          <button className={tab === "wishlist" ? "active" : ""} onClick={() => setTab("wishlist")}>
            Wishlist
          </button>
        </nav>
      </header>
      <main>
        {tab === "recommend" && <RecommendationView />}
        {tab === "record" && <RecordWatchedView />}
        {tab === "wishlist" && <WishlistView />}
      </main>
    </div>
  );
}

function RecommendationView() {
  const [session, setSession] = useState<RecommendationSession | null>(null);
  const [seed, setSeed] = useState("42");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);

  async function loadRecommendations() {
    setLoading(true);
    setStatus("");
    try {
      const query = seed.trim() ? `?strategy=hybrid&seed=${encodeURIComponent(seed.trim())}` : "?strategy=hybrid";
      const data = await api<RecommendationSession>(`/recommendations${query}`);
      setSession(data);
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }

  async function submitFeedback(item: RecommendationItem, feedbackType: string) {
    if (!session) return;
    setStatus("");
    try {
      await api(`/recommendations/${session.id}/items/${item.id}/feedback`, {
        method: "POST",
        body: JSON.stringify({ feedback_type: feedbackType })
      });
      setStatus("Saved");
    } catch (error) {
      setStatus(errorMessage(error));
    }
  }

  return (
    <section className="panel">
      <div className="toolbar">
        <label>
          Seed
          <input value={seed} onChange={(event) => setSeed(event.target.value)} inputMode="numeric" />
        </label>
        <button className="primary" onClick={loadRecommendations} disabled={loading}>
          {loading ? "Loading" : "Recommend"}
        </button>
        <StatusText value={status} />
      </div>
      <div className="movie-grid">
        {session?.items.map((item) => (
          <MovieCard key={item.id} item={item}>
            <div className="button-row">
              <button onClick={() => submitFeedback(item, "want_to_watch")}>Want</button>
              <button onClick={() => submitFeedback(item, "maybe_later")}>Later</button>
              <button onClick={() => submitFeedback(item, "not_interested")}>No</button>
            </div>
          </MovieCard>
        ))}
      </div>
    </section>
  );
}

function RecordWatchedView() {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<SearchCandidate[]>([]);
  const [selected, setSelected] = useState<SearchCandidate | null>(null);
  const [form, setForm] = useState<RecordForm>({
    watched_date: today,
    rating: "4.0",
    quality: "1080p",
    comment: "",
    sheet: String(new Date().getFullYear())
  });
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);

  async function search(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setStatus("");
    setSelected(null);
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
      await api("/viewing-history", {
        method: "POST",
        body: JSON.stringify({
          douban_subject_id: selected.subject_id,
          watched_date: form.watched_date,
          rating: Number(form.rating),
          quality: form.quality || null,
          comment: form.comment || null,
          sheet: form.sheet
        })
      });
      setStatus("Recorded");
      setCandidates([]);
      setSelected(null);
      setQuery("");
      setForm((current) => ({ ...current, comment: "" }));
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
          <label>
            Movie
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Enter name or id" />
          </label>
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
          <input value={form.rating} onChange={(event) => setForm({ ...form, rating: event.target.value })} />
        </label>
        <label>
          Quality
          <input value={form.quality} onChange={(event) => setForm({ ...form, quality: event.target.value })} />
        </label>
        <label>
          Sheet
          <input value={form.sheet} onChange={(event) => setForm({ ...form, sheet: event.target.value })} />
        </label>
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

function WishlistView() {
  const [items, setItems] = useState<WishlistItem[]>([]);
  const [status, setStatus] = useState("");
  const [loaded, setLoaded] = useState(false);

  async function loadWishlist() {
    setStatus("");
    try {
      const data = await api<{ items: WishlistItem[] }>("/wishlist");
      setItems(data.items);
      setLoaded(true);
    } catch (error) {
      setStatus(errorMessage(error));
    }
  }

  return (
    <section className="panel">
      <div className="toolbar">
        <button className="primary" onClick={loadWishlist}>
          Refresh
        </button>
        <StatusText value={status} />
      </div>
      <div className="movie-grid">
        {items.map((item) => (
          <article className="movie-card" key={item.id}>
            <h3>{item.movie.title}</h3>
            <p>{[item.movie.year || null, item.movie.director].filter(Boolean).join(" · ")}</p>
            <p>Rating {item.movie.douban_rating.toFixed(1)}</p>
            <a href={item.movie.douban_url} target="_blank" rel="noreferrer">
              Douban
            </a>
          </article>
        ))}
      </div>
      {loaded && items.length === 0 && <p className="empty">No active wishlist items.</p>}
    </section>
  );
}

function MovieCard({ item, children }: { item: RecommendationItem; children: React.ReactNode }) {
  const cast = useMemo(() => item.movie.main_cast.slice(0, 3).join(", "), [item.movie.main_cast]);
  return (
    <article className="movie-card">
      <div className="rank-line">
        <span>#{item.rank}</span>
        <span>{item.slot_type}</span>
      </div>
      <h3>{item.movie.title}</h3>
      <p>{[item.movie.year || null, item.movie.director].filter(Boolean).join(" · ")}</p>
      <p>{cast}</p>
      <div className="metric-row">
        <span>Douban {item.movie.douban_rating.toFixed(1)}</span>
        <span>Score {item.score.toFixed(2)}</span>
      </div>
      <a href={item.movie.douban_url} target="_blank" rel="noreferrer">
        Douban
      </a>
      {children}
    </article>
  );
}

function StatusText({ value }: { value: string }) {
  return value ? <span className="status">{value}</span> : null;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {})
    }
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || response.statusText);
  }
  return response.json() as Promise<T>;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Request failed";
}

createRoot(document.getElementById("root")!).render(<App />);
