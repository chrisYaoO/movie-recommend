from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import re
from typing import Protocol

from backend.app.models.domain import DoubanMatchInput
from backend.app.config import load_local_env, resolve_postgres_dsn
from backend.app.services.matching_service import (
    DoubanHttpSearchAdapter,
    DoubanSearchAdapter,
)

load_local_env()


@dataclass(frozen=True)
class MovieSearchCandidate:
    subject_id: str
    title: str
    year: int | None
    director: str | None
    url: str | None


class LocalMovieSearchLookup(Protocol):
    def find_by_subject_id(self, subject_id: str) -> MovieSearchCandidate | None: ...


class PostgresLocalMovieSearchLookup:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def find_by_subject_id(self, subject_id: str) -> MovieSearchCandidate | None:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT douban_subject_id, douban_url, title, year, directors
                FROM movies
                WHERE douban_subject_id = %s
                """,
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        directors = _tuple_from_json_value(row["directors"])
        return MovieSearchCandidate(
            subject_id=str(row["douban_subject_id"]),
            title=str(row["title"]),
            year=int(row["year"]) if row["year"] is not None else None,
            director=", ".join(directors) if directors else None,
            url=str(row["douban_url"]) if row["douban_url"] else _douban_subject_url(subject_id),
        )


class MovieSearchService:
    def __init__(
        self,
        adapter: DoubanSearchAdapter,
        limit: int = 5,
        local_lookup: LocalMovieSearchLookup | None = None,
    ) -> None:
        self.adapter = adapter
        self.limit = limit
        self.local_lookup = local_lookup

    def search(self, query: str) -> list[MovieSearchCandidate]:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise ValueError("query is required")

        subject_id = extract_douban_subject_id(normalized_query)
        if subject_id is not None:
            return [self._candidate_from_subject_id(subject_id)]

        match_input = DoubanMatchInput(
            source_raw_id="api-search",
            source_sheet_name="api",
            source_row_number=0,
            title=normalized_query,
            strategy="metadata",
        )
        results = self.adapter.search(match_input)[: self.limit]
        return [
            MovieSearchCandidate(
                subject_id=result.subject_id,
                title=result.title,
                year=result.year,
                director=result.director,
                url=result.url,
            )
            for result in results
        ]

    def _candidate_from_subject_id(self, subject_id: str) -> MovieSearchCandidate:
        local_candidate = self.local_lookup.find_by_subject_id(subject_id) if self.local_lookup else None
        if local_candidate is not None:
            return local_candidate
        return MovieSearchCandidate(
            subject_id=subject_id,
            title=f"Douban subject {subject_id}",
            year=None,
            director=None,
            url=_douban_subject_url(subject_id),
        )

    def to_response(self, query: str, candidates: list[MovieSearchCandidate]) -> dict:
        return {
            "query": query,
            "items": [asdict(candidate) for candidate in candidates],
        }


def create_movie_search_service() -> MovieSearchService:
    local_lookup = _create_local_lookup()
    return MovieSearchService(
        DoubanHttpSearchAdapter(),
        local_lookup=local_lookup,
    )


def extract_douban_subject_id(query: str) -> str | None:
    stripped = query.strip()
    if re.fullmatch(r"\d{5,}", stripped):
        return stripped
    match = re.search(r"(?:movie\.)?douban\.com/subject/(\d+)/?", stripped)
    if match is not None:
        return match.group(1)
    return None


def _create_local_lookup() -> LocalMovieSearchLookup | None:
    try:
        dsn = resolve_postgres_dsn(os.getenv("MOVIES_POSTGRES_DSN"), ".env")
    except ValueError:
        return None
    return PostgresLocalMovieSearchLookup(dsn)


def _douban_subject_url(subject_id: str) -> str:
    return f"https://movie.douban.com/subject/{subject_id}/"


def _tuple_from_json_value(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        import json

        value = json.loads(value)
    return tuple(str(item) for item in value)
