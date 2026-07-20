import { useEffect, useState } from "react";
import type { Movie } from "../types";
import { processingStatusLabel } from "../utils/movie";
import { UndoIcon } from "./Icons";

export function SharedMovieCard({
  movie,
  badge,
  score,
  sourceLabel,
  why,
  processedStatus,
  loadPoster = true,
  onUndoProcessed,
  children
}: {
  movie: Movie;
  badge: string;
  score?: number;
  sourceLabel?: string;
  why?: string[];
  processedStatus?: string | null;
  loadPoster?: boolean;
  onUndoProcessed?: () => void;
  children?: React.ReactNode;
}) {
  const [posterFailed, setPosterFailed] = useState(false);
  const [posterLoaded, setPosterLoaded] = useState(false);
  const directors = movie.directors?.length ? movie.directors : [movie.director];
  const cast = (movie.cast?.length ? movie.cast : movie.main_cast).slice(0, 3).join(", ");
  const statusText = processedStatus ? processingStatusLabel(processedStatus) : null;
  const showPoster = loadPoster && Boolean(movie.poster_url) && !posterFailed;
  const recommendationSource = recommendationSourceLabel(sourceLabel || badge);

  useEffect(() => {
    setPosterFailed(false);
    setPosterLoaded(false);
  }, [movie.poster_url]);

  return (
    <article className={processedStatus ? "movie-card processed" : "movie-card"}>
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
      <div className="movie-card-body">
        <h3>
          <a href={movie.douban_url} target="_blank" rel="noreferrer">
            {movie.title}
          </a>
        </h3>
        <p className="movie-credits">{[movie.year || null, directors.join(", ")].filter(Boolean).join(" · ")}</p>
        {cast && <p className="movie-cast">{cast}</p>}
      </div>
      <div className="card-meta-row">
        <span>Douban {movie.douban_rating.toFixed(1)}</span>
        {score !== undefined && <span>Score {score}</span>}
        <span>{badge}</span>
      </div>
      {why && why.length > 0 && (
        <details className="why-details">
          <summary aria-label={`Show recommendation details from ${recommendationSource}`}>
            <span>Recommend from</span>
            <strong>{recommendationSource}</strong>
          </summary>
          <ul>
            {why.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </details>
      )}
      {statusText && (
        <div className="processed-row">
          <p className="processed-label">{statusText}</p>
          {onUndoProcessed && (
            <button className="icon-button" onClick={onUndoProcessed} aria-label={`Undo ${statusText}`} title="Undo">
              <UndoIcon />
            </button>
          )}
        </div>
      )}
      <div className="movie-card-actions">{children}</div>
    </article>
  );
}

function recommendationSourceLabel(value: string) {
  return value.replace(/^recommend(?:ed|ation)?\s+from\s*/i, "").trim() || value;
}
