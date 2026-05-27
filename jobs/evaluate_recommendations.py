from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
import sys
from typing import Iterable

from backend.app.models.domain import RecommendationSession
from backend.app.services.recommendation_service import PostgresRecommendationRepository, RecommendationService
from jobs.import_auto_matched_history import resolve_postgres_dsn


@dataclass(frozen=True)
class RecommendationEvaluationItem:
    run_index: int
    rank: int
    slot_type: str
    movie_id: str
    title: str
    year: int
    score: float
    douban_rating: float
    watched: bool
    pool_sources: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationEvaluationSummary:
    runs: int
    total_items: int
    unique_movies: int
    duplicate_in_session_count: int
    watched_leak_count: int
    average_douban_rating: float
    slot_mix: dict[str, int]
    source_mix: dict[str, int]
    repeated_movies: dict[str, int]


@dataclass(frozen=True)
class CandidatePoolHealthSummary:
    active_pool_entries: int
    active_unique_movies: int
    eligible_unique_movies: int
    watched_candidate_count: int
    active_wishlist_candidate_count: int
    not_interested_candidate_count: int
    average_douban_rating: float
    queue_status_counts: dict[str, int]
    active_source_mix: dict[str, int]
    metadata_missing_counts: dict[str, int]


@dataclass(frozen=True)
class RecommendationEvaluationResult:
    strategy: str
    items: tuple[RecommendationEvaluationItem, ...]
    summary: RecommendationEvaluationSummary
    pool_health: CandidatePoolHealthSummary | None = None


def evaluate_recommendations(
    service: RecommendationService,
    repository: PostgresRecommendationRepository,
    strategy: str,
    runs: int,
    seed: int | None = None,
) -> RecommendationEvaluationResult:
    watched_movie_ids = _watched_movie_ids(repository)
    pool_sources_by_movie_id = _pool_sources_by_movie_id(repository)

    items: list[RecommendationEvaluationItem] = []
    sessions = [service.recommend(strategy, explore_seed=_run_seed(seed, run_index)) for run_index in range(runs)]
    for run_index, session in enumerate(sessions, start=1):
        items.extend(
            _evaluation_items_for_session(
                session,
                run_index,
                watched_movie_ids,
                pool_sources_by_movie_id,
            )
        )

    return RecommendationEvaluationResult(
        strategy=strategy,
        items=tuple(items),
        summary=_summarize(items, runs),
        pool_health=collect_candidate_pool_health(repository),
    )


def render_text(result: RecommendationEvaluationResult) -> str:
    lines: list[str] = []
    current_run: int | None = None
    for item in result.items:
        if item.run_index != current_run:
            current_run = item.run_index
            if lines:
                lines.append("")
            lines.append(f"run {current_run}")
        sources = ", ".join(item.pool_sources) if item.pool_sources else "-"
        watched = "true" if item.watched else "false"
        lines.append(
            f"{item.rank}. {item.slot_type:<7} {item.title} ({item.year or '-'}) "
            f"score={item.score:.3f} rating={item.douban_rating:.1f} "
            f"watched={watched} source={sources}"
        )

    summary = result.summary
    if result.pool_health is not None:
        pool = result.pool_health
        lines.extend(
            [
                "",
                "candidate_pool_health",
                f"active_pool_entries={pool.active_pool_entries}",
                f"active_unique_movies={pool.active_unique_movies}",
                f"eligible_unique_movies={pool.eligible_unique_movies}",
                f"watched_candidate_count={pool.watched_candidate_count}",
                f"active_wishlist_candidate_count={pool.active_wishlist_candidate_count}",
                f"not_interested_candidate_count={pool.not_interested_candidate_count}",
                f"average_douban_rating={pool.average_douban_rating:.3f}",
                "queue_status_counts:",
                *_format_counter(pool.queue_status_counts),
                "active_source_mix:",
                *_format_counter(pool.active_source_mix),
                "metadata_missing_counts:",
                *_format_counter(pool.metadata_missing_counts),
            ]
        )

    lines.extend(
        [
            "",
            "summary",
            f"strategy={result.strategy}",
            f"runs={summary.runs}",
            f"total_items={summary.total_items}",
            f"unique_movies={summary.unique_movies}",
            f"duplicate_in_session_count={summary.duplicate_in_session_count}",
            f"watched_leak_count={summary.watched_leak_count}",
            f"average_douban_rating={summary.average_douban_rating:.3f}",
            "slot_mix:",
            *_format_counter(summary.slot_mix),
            "source_mix:",
            *_format_counter(summary.source_mix),
            "repeated_movies:",
            *_format_counter(summary.repeated_movies),
        ]
    )
    return "\n".join(lines)


def collect_candidate_pool_health(repository: PostgresRecommendationRepository) -> CandidatePoolHealthSummary:
    count_row = repository.connection.execute(
        """
        SELECT
            COUNT(*) AS active_pool_entries,
            COUNT(DISTINCT cp.movie_id) AS active_unique_movies,
            COUNT(DISTINCT cp.movie_id) FILTER (
                WHERE NOT EXISTS (
                    SELECT 1 FROM viewing_history vh WHERE vh.movie_id = cp.movie_id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM wishlist w WHERE w.movie_id = cp.movie_id AND w.status = 'active'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM feedback f
                    WHERE f.movie_id = cp.movie_id AND f.feedback_type = 'not_interested'
                )
            ) AS eligible_unique_movies,
            COUNT(DISTINCT cp.movie_id) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM viewing_history vh WHERE vh.movie_id = cp.movie_id
                )
            ) AS watched_candidate_count,
            COUNT(DISTINCT cp.movie_id) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM wishlist w WHERE w.movie_id = cp.movie_id AND w.status = 'active'
                )
            ) AS active_wishlist_candidate_count,
            COUNT(DISTINCT cp.movie_id) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM feedback f
                    WHERE f.movie_id = cp.movie_id AND f.feedback_type = 'not_interested'
                )
            ) AS not_interested_candidate_count,
            COALESCE(AVG(m.douban_rating), 0) AS average_douban_rating,
            COUNT(DISTINCT cp.movie_id) FILTER (WHERE m.year IS NULL OR m.year = 0) AS missing_year,
            COUNT(DISTINCT cp.movie_id) FILTER (WHERE m.douban_rating IS NULL OR m.douban_rating = 0) AS missing_rating,
            COUNT(DISTINCT cp.movie_id) FILTER (WHERE m.douban_vote_count IS NULL OR m.douban_vote_count = 0) AS missing_vote_count,
            COUNT(DISTINCT cp.movie_id) FILTER (WHERE jsonb_array_length(m.directors) = 0) AS missing_directors,
            COUNT(DISTINCT cp.movie_id) FILTER (WHERE jsonb_array_length(m.actors) = 0) AS missing_actors,
            COUNT(DISTINCT cp.movie_id) FILTER (WHERE jsonb_array_length(m.genres) = 0) AS missing_genres,
            COUNT(DISTINCT cp.movie_id) FILTER (WHERE jsonb_array_length(m.countries) = 0) AS missing_countries,
            COUNT(DISTINCT cp.movie_id) FILTER (WHERE m.douban_url IS NULL OR m.douban_url = '') AS missing_douban_url
        FROM candidate_pool cp
        JOIN movies m ON m.id = cp.movie_id
        WHERE cp.active = TRUE
        """
    ).fetchone()
    queue_rows = repository.connection.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM candidate_subject_queue
        GROUP BY status
        ORDER BY status
        """
    ).fetchall()
    source_rows = repository.connection.execute(
        """
        SELECT source_type, COUNT(*) AS count
        FROM candidate_pool
        WHERE active = TRUE
        GROUP BY source_type
        ORDER BY source_type
        """
    ).fetchall()

    return CandidatePoolHealthSummary(
        active_pool_entries=int(count_row["active_pool_entries"] or 0),
        active_unique_movies=int(count_row["active_unique_movies"] or 0),
        eligible_unique_movies=int(count_row["eligible_unique_movies"] or 0),
        watched_candidate_count=int(count_row["watched_candidate_count"] or 0),
        active_wishlist_candidate_count=int(count_row["active_wishlist_candidate_count"] or 0),
        not_interested_candidate_count=int(count_row["not_interested_candidate_count"] or 0),
        average_douban_rating=float(count_row["average_douban_rating"] or 0),
        queue_status_counts={str(row["status"]): int(row["count"]) for row in queue_rows},
        active_source_mix={str(row["source_type"]): int(row["count"]) for row in source_rows},
        metadata_missing_counts={
            "year": int(count_row["missing_year"] or 0),
            "rating": int(count_row["missing_rating"] or 0),
            "vote_count": int(count_row["missing_vote_count"] or 0),
            "directors": int(count_row["missing_directors"] or 0),
            "actors": int(count_row["missing_actors"] or 0),
            "genres": int(count_row["missing_genres"] or 0),
            "countries": int(count_row["missing_countries"] or 0),
            "douban_url": int(count_row["missing_douban_url"] or 0),
        },
    )


def _evaluation_items_for_session(
    session: RecommendationSession,
    run_index: int,
    watched_movie_ids: set[str],
    pool_sources_by_movie_id: dict[str, tuple[str, ...]],
) -> list[RecommendationEvaluationItem]:
    return [
        RecommendationEvaluationItem(
            run_index=run_index,
            rank=item.rank,
            slot_type=item.slot_type.value,
            movie_id=item.movie.id,
            title=item.movie.title,
            year=item.movie.year,
            score=item.score,
            douban_rating=item.movie.douban_rating,
            watched=item.movie.id in watched_movie_ids,
            pool_sources=pool_sources_by_movie_id.get(item.movie.id, ()),
        )
        for item in session.items
    ]


def _summarize(
    items: Iterable[RecommendationEvaluationItem],
    runs: int,
) -> RecommendationEvaluationSummary:
    item_list = list(items)
    movie_counts = Counter(item.movie_id for item in item_list)
    title_by_movie_id = {item.movie_id: item.title for item in item_list}
    duplicate_in_session_count = 0
    for run_index in {item.run_index for item in item_list}:
        run_movie_ids = [item.movie_id for item in item_list if item.run_index == run_index]
        duplicate_in_session_count += len(run_movie_ids) - len(set(run_movie_ids))

    source_mix: Counter[str] = Counter()
    for item in item_list:
        if item.pool_sources:
            source_mix.update(source.split(":", 1)[0] for source in item.pool_sources)
        else:
            source_mix["<none>"] += 1

    repeated_movies = {
        title_by_movie_id[movie_id]: count
        for movie_id, count in movie_counts.items()
        if count > 1
    }
    average_rating = (
        sum(item.douban_rating for item in item_list) / len(item_list)
        if item_list
        else 0.0
    )
    return RecommendationEvaluationSummary(
        runs=runs,
        total_items=len(item_list),
        unique_movies=len(movie_counts),
        duplicate_in_session_count=duplicate_in_session_count,
        watched_leak_count=sum(1 for item in item_list if item.watched),
        average_douban_rating=average_rating,
        slot_mix=dict(Counter(item.slot_type for item in item_list)),
        source_mix=dict(source_mix),
        repeated_movies=dict(sorted(repeated_movies.items(), key=lambda item: (-item[1], item[0]))),
    )


def _format_counter(values: dict[str, int]) -> list[str]:
    if not values:
        return ["  <none>=0"]
    return [f"  {key}={value}" for key, value in sorted(values.items())]


def _watched_movie_ids(repository: PostgresRecommendationRepository) -> set[str]:
    rows = repository.connection.execute("SELECT DISTINCT movie_id FROM viewing_history").fetchall()
    return {str(row["movie_id"]) for row in rows}


def _pool_sources_by_movie_id(repository: PostgresRecommendationRepository) -> dict[str, tuple[str, ...]]:
    rows = repository.connection.execute(
        """
        SELECT movie_id, source_type, source_ref
        FROM candidate_pool
        WHERE active = TRUE
        ORDER BY source_type, source_ref
        """
    ).fetchall()
    sources: dict[str, list[str]] = {}
    for row in rows:
        sources.setdefault(str(row["movie_id"]), []).append(f"{row['source_type']}:{row['source_ref']}")
    return {movie_id: tuple(values) for movie_id, values in sources.items()}


def _run_seed(seed: int | None, run_index: int) -> int | None:
    if seed is None:
        return None
    return seed + run_index


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Evaluate recommendation output quality.")
    parser.add_argument("--strategy", default="hybrid", choices=("hybrid", "popularity", "content"))
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--config-path", default=".env")
    parser.add_argument("--seed", type=int, default=None, help="Seed controlled randomness for explore slots.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    if args.runs <= 0:
        parser.error("--runs must be positive")

    dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    repository = PostgresRecommendationRepository(dsn)
    try:
        service = RecommendationService(repository)
        result = evaluate_recommendations(service, repository, args.strategy, args.runs, seed=args.seed)
    finally:
        repository.close()

    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        print(render_text(result))


if __name__ == "__main__":
    main()
