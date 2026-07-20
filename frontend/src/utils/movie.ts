import { BASELINE_HYBRID_TOTAL } from "../constants";
import type { Movie, RecommendationItem, SearchCandidate } from "../types";

export function normalizedScore(score: number) {
  return Math.round((score / BASELINE_HYBRID_TOTAL) * 100);
}

export function recommendationReason(item: RecommendationItem) {
  const lines = [
    item.slot_type === "explore" ? "Explore slot: keeps recommendations diverse." : "Exploit slot: high match to your history.",
    item.source_label || item.source_ref ? `Source: ${item.source_label || item.source_ref}` : null,
    `Raw score: ${item.score.toFixed(2)}`
  ];
  const components = Object.entries(item.score_components || {})
    .filter(([, value]) => typeof value === "number")
    .sort(([, left], [, right]) => Math.abs(Number(right)) - Math.abs(Number(left)))
    .slice(0, 3)
    .map(([key, value]) => `${formatScoreComponentName(key)}: ${formatScoreComponentValue(value)}`);
  return [...lines, ...components].filter(Boolean) as string[];
}

export function processingStatusLabel(status: string) {
  return (
    {
      watched: "Watched",
      added_to_wishlist: "Added to wishlist",
      not_interested: "Not interested",
      maybe_later: "Maybe later"
    }[status] || status
  );
}

export function movieMatchesFilter(movie: Movie, filterText: string) {
  const query = filterText.trim().toLocaleLowerCase();
  if (!query) return true;
  return movieFilterText(movie).includes(query);
}

export function searchCandidateFromMovie(movie: Movie): SearchCandidate {
  return {
    subject_id: subjectIdFromDoubanUrl(movie.douban_url) || movie.id,
    title: movie.title,
    year: movie.year,
    director: movie.director,
    url: movie.douban_url
  };
}

function formatScoreComponentName(value: string) {
  return value.replace(/_/g, " ");
}

function formatScoreComponentValue(value: unknown) {
  return typeof value === "number" ? Number(value).toFixed(2) : String(value);
}

function movieFilterText(movie: Movie) {
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

function subjectIdFromDoubanUrl(url: string) {
  return url.match(/\/subject\/([^/]+)\//)?.[1] || null;
}
