from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
import json
import re
import sys
import time
from typing import Callable, Protocol
from urllib.request import Request, urlopen

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanDetailAdapter,
    DoubanSeleniumDetailAdapter,
)
from jobs.import_auto_matched_history import resolve_postgres_dsn

DOUBAN_TOP250_SOURCE = "douban_top250"
DOUBAN_RECOMMENDATION_SOURCE = "douban_recommendation"
QUEUE_STATUS_ENRICHED = "enriched"
QUEUE_STATUS_FAILED = "failed"
QUEUE_STATUS_PENDING = "pending"
HISTORY_RECOMMENDATION_STATUS_COMPLETED = "completed"
HISTORY_RECOMMENDATION_STATUS_FAILED = "failed"
StatusWriter = Callable[[str], None]


class DoubanPageDetailAdapter(Protocol):
    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        pass

    @property
    def last_page_source(self) -> str | None:
        pass


@dataclass(frozen=True)
class Top250DiscoverySummary:
    discovered_count: int
    inserted_count: int


@dataclass(frozen=True)
class CandidateQueueProcessSummary:
    attempted_count: int
    enriched_count: int
    existing_movie_count: int
    candidate_pool_inserted_count: int
    recommendation_discovered_count: int
    recommendation_inserted_count: int
    failed_count: int


@dataclass(frozen=True)
class HistoryRecommendationDiscoverySummary:
    watched_movie_count: int
    attempted_count: int
    recommendation_discovered_count: int
    recommendation_inserted_count: int
    failed_count: int


class DoubanTop250Client:
    def __init__(
        self,
        base_url: str = "https://movie.douban.com/top250",
        timeout_seconds: float = 20.0,
        delay_seconds: float = 1.0,
        user_agent: str = "Mozilla/5.0",
    ) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.delay_seconds = delay_seconds
        self.user_agent = user_agent
        self.last_request_at = 0.0

    def fetch_page(self, start: int) -> str:
        self._throttle()
        request = Request(
            f"{self.base_url}?start={start}&filter=",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
        self.last_request_at = time.monotonic()
        return html

    def _throttle(self) -> None:
        if self.last_request_at <= 0:
            return
        remaining = self.delay_seconds - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining)


class SeleniumDetailPageAdapter:
    def __init__(self, inner: DoubanSeleniumDetailAdapter) -> None:
        self.inner = inner
        self._last_page_source: str | None = None

    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        detail = self.inner.fetch(subject_id)
        driver = getattr(self.inner, "driver", None)
        self._last_page_source = getattr(driver, "page_source", None)
        return detail

    @property
    def last_page_source(self) -> str | None:
        return self._last_page_source

    def close(self) -> None:
        self.inner.close()


def discover_top250_subjects(
    repository: ViewingHistoryRepository,
    client: DoubanTop250Client,
) -> Top250DiscoverySummary:
    subjects: list[str] = []
    for start in range(0, 250, 25):
        subjects.extend(parse_top250_subject_ids(client.fetch_page(start)))

    unique_subjects = list(dict.fromkeys(subjects))
    inserted_count = 0
    for index, subject_id in enumerate(unique_subjects[:250], start=1):
        if repository.upsert_candidate_subject(
            subject_id,
            source_type=DOUBAN_TOP250_SOURCE,
            source_ref=f"top{index}",
        ):
            inserted_count += 1

    return Top250DiscoverySummary(discovered_count=len(unique_subjects[:250]), inserted_count=inserted_count)


def process_candidate_queue(
    repository: ViewingHistoryRepository,
    detail_adapter: DoubanPageDetailAdapter,
    limit: int | None = None,
    recommendation_parser: Callable[[str, str], list[str]] | None = None,
    status_writer: StatusWriter | None = None,
    queue_status: str = QUEUE_STATUS_PENDING,
) -> CandidateQueueProcessSummary:
    if recommendation_parser is None:
        recommendation_parser = parse_recommended_subject_ids

    total_count = repository.count_candidate_subjects_by_status(queue_status)
    pending_items = repository.find_candidate_subjects_by_status(queue_status, limit=limit)
    _write_status(
        status_writer,
        f"[queue] status={queue_status}, remaining={total_count}, selected={len(pending_items)}, limit={limit}",
    )

    attempted_count = 0
    enriched_count = 0
    existing_movie_count = 0
    candidate_pool_inserted_count = 0
    recommendation_discovered_count = 0
    recommendation_inserted_count = 0
    failed_count = 0

    for item in pending_items:
        attempted_count += 1
        _write_status(
            status_writer,
            "[queue] "
            f"{attempted_count}/{total_count} "
            f"subject={item.douban_subject_id}, "
            f"source={item.source_type}:{item.source_ref}",
        )
        try:
            existing = repository.find_movie_by_subject_id(item.douban_subject_id)
            if existing is None:
                _write_status(status_writer, f"[detail] fetch subject={item.douban_subject_id}")
                detail = detail_adapter.fetch(item.douban_subject_id)
                movie = repository.upsert_movie_detail(detail)
                enriched_count += 1
                page_source = detail_adapter.last_page_source
                source_label = detail.title
                _write_status(
                    status_writer,
                    f"[detail] fetched title={detail.title}, year={detail.year or '-'}",
                )
            else:
                movie = existing
                existing_movie_count += 1
                page_source = None
                source_label = existing.title
                _write_status(
                    status_writer,
                    f"[detail] existing movie_id={existing.id}, title={existing.title}",
                )

            if repository.upsert_candidate_pool_entry(movie.id, item.source_type, item.source_ref):
                candidate_pool_inserted_count += 1
                _write_status(status_writer, f"[pool] inserted movie_id={movie.id}")
            else:
                _write_status(status_writer, f"[pool] already active movie_id={movie.id}")

            if item.source_type == DOUBAN_TOP250_SOURCE and page_source:
                recommended_ids = recommendation_parser(page_source, item.douban_subject_id)
                recommendation_discovered_count += len(recommended_ids)
                inserted_this_subject = 0
                for recommended_id in recommended_ids:
                    if repository.upsert_candidate_subject(
                        recommended_id,
                        source_type=DOUBAN_RECOMMENDATION_SOURCE,
                        source_ref=f"recommended_from:{item.douban_subject_id}",
                        source_subject_id=item.douban_subject_id,
                        source_label=f"recommended from {source_label}",
                    ):
                        recommendation_inserted_count += 1
                        inserted_this_subject += 1
                _write_status(
                    status_writer,
                    f"[recommendation] discovered={len(recommended_ids)}, inserted={inserted_this_subject}",
                )

            repository.mark_candidate_subject_status(item.douban_subject_id, QUEUE_STATUS_ENRICHED)
            remaining_count = repository.count_candidate_subjects_by_status(queue_status)
            _write_status(
                status_writer,
                f"[queue] status=enriched subject={item.douban_subject_id}, remaining_{queue_status}={remaining_count}",
            )
        except Exception as exc:
            failed_count += 1
            repository.mark_candidate_subject_status(item.douban_subject_id, QUEUE_STATUS_FAILED, str(exc))
            remaining_count = repository.count_candidate_subjects_by_status(queue_status)
            _write_status(
                status_writer,
                f"[queue] status=failed subject={item.douban_subject_id}, remaining_{queue_status}={remaining_count}, error={exc}",
            )

    _write_status(
        status_writer,
        "[summary] "
        f"attempted={attempted_count}, "
        f"enriched={enriched_count}, "
        f"existing={existing_movie_count}, "
        f"pool_inserted={candidate_pool_inserted_count}, "
        f"recommendation_discovered={recommendation_discovered_count}, "
        f"recommendation_inserted={recommendation_inserted_count}, "
        f"failed={failed_count}",
    )

    return CandidateQueueProcessSummary(
        attempted_count=attempted_count,
        enriched_count=enriched_count,
        existing_movie_count=existing_movie_count,
        candidate_pool_inserted_count=candidate_pool_inserted_count,
        recommendation_discovered_count=recommendation_discovered_count,
        recommendation_inserted_count=recommendation_inserted_count,
        failed_count=failed_count,
    )


def discover_history_recommendations(
    repository: ViewingHistoryRepository,
    detail_adapter: DoubanPageDetailAdapter,
    limit: int | None = None,
    recommendation_parser: Callable[[str, str], list[str]] | None = None,
    status_writer: StatusWriter | None = None,
) -> HistoryRecommendationDiscoverySummary:
    if recommendation_parser is None:
        recommendation_parser = parse_recommended_subject_ids

    total_count = repository.count_unprocessed_watched_movies_for_history_recommendations()
    watched_movies = repository.find_unprocessed_watched_movies_for_history_recommendations(limit=limit)
    _write_status(
        status_writer,
        f"[history-recommendation] remaining={total_count}, selected={len(watched_movies)}, limit={limit}",
    )

    attempted_count = 0
    recommendation_discovered_count = 0
    recommendation_inserted_count = 0
    failed_count = 0

    for movie in watched_movies:
        attempted_count += 1
        _write_status(
            status_writer,
            "[history-recommendation] "
            f"{attempted_count}/{total_count} "
            f"subject={movie.douban_subject_id}, title={movie.title}",
        )
        try:
            detail = detail_adapter.fetch(movie.douban_subject_id)
            page_source = detail_adapter.last_page_source
            if not page_source:
                failed_count += 1
                repository.mark_history_recommendation_discovery_status(
                    movie.douban_subject_id,
                    HISTORY_RECOMMENDATION_STATUS_FAILED,
                    "no page source",
                )
                remaining_count = repository.count_unprocessed_watched_movies_for_history_recommendations()
                _write_status(
                    status_writer,
                    "[history-recommendation] "
                    f"no page source subject={movie.douban_subject_id}, "
                    f"remaining_watched={remaining_count}",
                )
                continue

            recommended_ids = recommendation_parser(page_source, movie.douban_subject_id)
            recommendation_discovered_count += len(recommended_ids)
            inserted_this_movie = 0
            for recommended_id in recommended_ids:
                if repository.upsert_candidate_subject(
                    recommended_id,
                    source_type=DOUBAN_RECOMMENDATION_SOURCE,
                    source_ref=f"recommended_from:{movie.douban_subject_id}",
                    source_subject_id=movie.douban_subject_id,
                    source_label=f"recommended from {detail.title}",
                ):
                    recommendation_inserted_count += 1
                    inserted_this_movie += 1
            repository.mark_history_recommendation_discovery_status(
                movie.douban_subject_id,
                HISTORY_RECOMMENDATION_STATUS_COMPLETED,
            )
            remaining_count = repository.count_unprocessed_watched_movies_for_history_recommendations()
            _write_status(
                status_writer,
                "[history-recommendation] "
                f"discovered={len(recommended_ids)}, inserted={inserted_this_movie}, "
                f"subject={movie.douban_subject_id}, remaining_watched={remaining_count}",
            )
        except Exception as exc:
            failed_count += 1
            repository.mark_history_recommendation_discovery_status(
                movie.douban_subject_id,
                HISTORY_RECOMMENDATION_STATUS_FAILED,
                str(exc),
            )
            remaining_count = repository.count_unprocessed_watched_movies_for_history_recommendations()
            _write_status(
                status_writer,
                "[history-recommendation] "
                f"failed subject={movie.douban_subject_id}, "
                f"remaining_watched={remaining_count}, error={exc}",
            )

    _write_status(
        status_writer,
        "[summary] "
        f"attempted={attempted_count}, "
        f"recommendation_discovered={recommendation_discovered_count}, "
        f"recommendation_inserted={recommendation_inserted_count}, "
        f"failed={failed_count}",
    )
    return HistoryRecommendationDiscoverySummary(
        watched_movie_count=len(watched_movies),
        attempted_count=attempted_count,
        recommendation_discovered_count=recommendation_discovered_count,
        recommendation_inserted_count=recommendation_inserted_count,
        failed_count=failed_count,
    )


def parse_top250_subject_ids(html: str) -> list[str]:
    ids: list[str] = []
    for match in re.finditer(r"https://movie\.douban\.com/subject/(\d+)/", html):
        subject_id = match.group(1)
        if subject_id not in ids:
            ids.append(subject_id)
    return ids


def parse_recommended_subject_ids(html: str, current_subject_id: str) -> list[str]:
    if re.search(r"(?:\u559c\u6b22\u8fd9\u90e8(?:\u7535\u5f71|\u5267\u96c6)\u7684\u4eba\u4e5f\u559c\u6b22|鍠滄杩欓儴(?:鐢靛奖|鍓ч泦)鐨勪汉涔熷枩娆?)", html) is None:
        return []
    parser = DoubanRecommendationSectionParser(current_subject_id)
    parser.feed(html)
    return parser.ids


class DoubanRecommendationSectionParser(HTMLParser):
    def __init__(self, current_subject_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.current_subject_id = current_subject_id
        self.ids: list[str] = []
        self._recommendations_depth: int | None = None
        self._recommendations_bd_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div" and attributes.get("id") == "recommendations":
            self._recommendations_depth = 1
            return

        if self._recommendations_depth is None:
            return

        self._recommendations_depth += 1
        class_names = set((attributes.get("class") or "").split())
        if tag == "div" and "recommendations-bd" in class_names:
            self._recommendations_bd_depth = self._recommendations_depth
            return

        if tag != "a" or self._recommendations_bd_depth is None:
            return

        subject_id = _extract_subject_id_from_href(attributes.get("href") or "")
        if subject_id and subject_id != self.current_subject_id and subject_id not in self.ids:
            self.ids.append(subject_id)

    def handle_endtag(self, tag: str) -> None:
        if self._recommendations_depth is None:
            return

        if self._recommendations_bd_depth == self._recommendations_depth:
            self._recommendations_bd_depth = None
        self._recommendations_depth -= 1
        if self._recommendations_depth <= 0:
            self._recommendations_depth = None


def _extract_subject_id_from_href(href: str) -> str | None:
    match = re.search(r"https://movie\.douban\.com/subject/(\d+)/", href)
    if match is None:
        return None
    return match.group(1)


def _write_status(status_writer: StatusWriter | None, message: str) -> None:
    if status_writer is not None:
        status_writer(message)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Discover and enrich local candidate-pool movies.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    top250 = subparsers.add_parser("discover-top250", help="Queue Douban Top250 subject IDs.")
    top250.add_argument("--dsn", default=None)
    top250.add_argument("--config-path", default=".env")
    top250.add_argument("--timeout-seconds", type=float, default=20.0)
    top250.add_argument("--delay-seconds", type=float, default=1.0)

    process = subparsers.add_parser("process-queue", help="Enrich queued candidates and activate candidate_pool.")
    process.add_argument("--dsn", default=None)
    process.add_argument("--config-path", default=".env")
    process.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    process.add_argument("--timeout-seconds", type=float, default=20.0)
    process.add_argument("--delay-seconds", type=float, default=1.0)
    process.add_argument("--limit", type=int, default=None)
    process.add_argument(
        "--retry-failed",
        action="store_true",
        help="Process failed queue rows instead of pending rows.",
    )

    history_recommendations = subparsers.add_parser(
        "discover-history-recommendations",
        help="Queue one-layer Douban recommendations from watched movies.",
    )
    history_recommendations.add_argument("--dsn", default=None)
    history_recommendations.add_argument("--config-path", default=".env")
    history_recommendations.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    history_recommendations.add_argument("--timeout-seconds", type=float, default=20.0)
    history_recommendations.add_argument("--delay-seconds", type=float, default=1.0)
    history_recommendations.add_argument("--limit", type=int, default=None)

    args = parser.parse_args()
    dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    repository = PostgresViewingHistoryRepository(dsn)
    repository.initialize_schema()

    detail_adapter: DoubanDetailAdapter | None = None
    try:
        if args.command == "discover-top250":
            result = discover_top250_subjects(
                repository,
                DoubanTop250Client(timeout_seconds=args.timeout_seconds, delay_seconds=args.delay_seconds),
            )
        elif args.command == "process-queue":
            selenium = DoubanSeleniumDetailAdapter(
                timeout_seconds=args.timeout_seconds,
                delay_seconds=args.delay_seconds,
                chrome_binary_path=args.chrome_binary_path,
            )
            detail_adapter = selenium
            result = process_candidate_queue(
                repository,
                SeleniumDetailPageAdapter(selenium),
                limit=args.limit,
                status_writer=lambda message: print(message, file=sys.stderr, flush=True),
                queue_status=QUEUE_STATUS_FAILED if args.retry_failed else QUEUE_STATUS_PENDING,
            )
        else:
            selenium = DoubanSeleniumDetailAdapter(
                timeout_seconds=args.timeout_seconds,
                delay_seconds=args.delay_seconds,
                chrome_binary_path=args.chrome_binary_path,
            )
            detail_adapter = selenium
            result = discover_history_recommendations(
                repository,
                SeleniumDetailPageAdapter(selenium),
                limit=args.limit,
                status_writer=lambda message: print(message, file=sys.stderr, flush=True),
            )
    finally:
        if detail_adapter is not None and hasattr(detail_adapter, "close"):
            detail_adapter.close()
        repository.close()

    print(json.dumps({"summary": asdict(result)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
