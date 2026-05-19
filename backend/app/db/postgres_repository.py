from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from backend.app.db.repository import (
    CandidateSubjectQueueItem,
    PersistedMovie,
    PersistedViewingHistory,
    PersistViewingHistoryResult,
)
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail


class PostgresViewingHistoryRepository:
    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgresViewingHistoryRepository") from exc

        self.connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def initialize_schema(self) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS movies (
                    id UUID PRIMARY KEY,
                    douban_subject_id TEXT NOT NULL UNIQUE,
                    douban_url TEXT,
                    title TEXT NOT NULL,
                    year INTEGER,
                    directors JSONB NOT NULL DEFAULT '[]'::jsonb,
                    actors JSONB NOT NULL DEFAULT '[]'::jsonb,
                    genres JSONB NOT NULL DEFAULT '[]'::jsonb,
                    countries JSONB NOT NULL DEFAULT '[]'::jsonb,
                    douban_rating NUMERIC,
                    douban_vote_count INTEGER,
                    summary TEXT,
                    poster_url TEXT,
                    raw_douban_json JSONB NOT NULL,
                    metadata_status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS viewing_history (
                    id UUID PRIMARY KEY,
                    movie_id UUID NOT NULL REFERENCES movies(id),
                    watched_date DATE,
                    user_rating NUMERIC NOT NULL,
                    quality TEXT,
                    comment TEXT,
                    source_row_hash TEXT NOT NULL UNIQUE,
                    source_file TEXT NOT NULL,
                    source_row_number INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_subject_queue (
                    douban_subject_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_subject_id TEXT,
                    source_label TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_pool (
                    id UUID PRIMARY KEY,
                    movie_id UUID NOT NULL REFERENCES movies(id),
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    active BOOLEAN NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(movie_id, source_type, source_ref)
                )
                """
            )

    def persist_confirmed_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        detail: DoubanMovieDetail,
    ) -> PersistViewingHistoryResult:
        if not confirmed.source_row_hash:
            raise ValueError("source_row_hash is required when raw viewing history is not persisted")
        if confirmed.douban_subject_id != detail.subject_id:
            raise ValueError("confirmed subject id does not match detail subject id")

        with self.connection.transaction():
            movie = self.upsert_movie_detail(detail)
            history = self.upsert_viewing_history(confirmed, movie.id)
        return PersistViewingHistoryResult(movie=movie, history=history)

    def find_movie_by_subject_id(self, subject_id: str) -> PersistedMovie | None:
        row = self.connection.execute(
            "SELECT id, douban_subject_id, title FROM movies WHERE douban_subject_id = %s",
            (subject_id,),
        ).fetchone()
        if row is None:
            return None
        return PersistedMovie(
            id=str(row["id"]),
            douban_subject_id=str(row["douban_subject_id"]),
            title=str(row["title"]),
        )

    def upsert_movie_detail(self, detail: DoubanMovieDetail) -> PersistedMovie:
        existing = self.connection.execute(
            "SELECT id FROM movies WHERE douban_subject_id = %s",
            (detail.subject_id,),
        ).fetchone()
        movie_id = str(existing["id"]) if existing is not None else str(uuid4())
        now = datetime.now(timezone.utc)

        self.connection.execute(
            """
            INSERT INTO movies (
                id, douban_subject_id, douban_url, title, year,
                directors, actors, genres, countries,
                douban_rating, douban_vote_count, summary, poster_url,
                raw_douban_json, metadata_status, created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT(douban_subject_id) DO UPDATE SET
                douban_url = excluded.douban_url,
                title = excluded.title,
                year = excluded.year,
                directors = excluded.directors,
                actors = excluded.actors,
                genres = excluded.genres,
                countries = excluded.countries,
                douban_rating = excluded.douban_rating,
                douban_vote_count = excluded.douban_vote_count,
                summary = excluded.summary,
                poster_url = excluded.poster_url,
                raw_douban_json = excluded.raw_douban_json,
                metadata_status = excluded.metadata_status,
                updated_at = excluded.updated_at
            """,
            (
                movie_id,
                detail.subject_id,
                detail.url,
                detail.title,
                detail.year,
                _jsonb(detail.directors),
                _jsonb(detail.actors),
                _jsonb(detail.genres),
                _jsonb(detail.countries),
                detail.douban_rating,
                detail.douban_vote_count,
                detail.summary,
                detail.poster_url,
                _jsonb(asdict(detail)),
                "enriched",
                now,
                now,
            ),
        )
        return PersistedMovie(id=movie_id, douban_subject_id=detail.subject_id, title=detail.title)

    def upsert_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        movie_id: str,
    ) -> PersistedViewingHistory:
        if not confirmed.source_row_hash:
            raise ValueError("source_row_hash is required when raw viewing history is not persisted")

        existing = self.connection.execute(
            "SELECT id FROM viewing_history WHERE source_row_hash = %s",
            (confirmed.source_row_hash,),
        ).fetchone()
        history_id = str(existing["id"]) if existing is not None else str(uuid4())
        now = datetime.now(timezone.utc)

        self.connection.execute(
            """
            INSERT INTO viewing_history (
                id, movie_id, watched_date, user_rating, quality, comment,
                source_row_hash, source_file, source_row_number, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(source_row_hash) DO UPDATE SET
                movie_id = excluded.movie_id,
                watched_date = excluded.watched_date,
                user_rating = excluded.user_rating,
                quality = excluded.quality,
                comment = excluded.comment,
                source_file = excluded.source_file,
                source_row_number = excluded.source_row_number,
                updated_at = excluded.updated_at
            """,
            (
                history_id,
                movie_id,
                confirmed.watched_date,
                confirmed.user_rating,
                confirmed.quality,
                confirmed.comment,
                confirmed.source_row_hash,
                confirmed.source_file,
                confirmed.source_row_number,
                now,
                now,
            ),
        )
        return PersistedViewingHistory(
            id=history_id,
            movie_id=movie_id,
            source_row_hash=confirmed.source_row_hash,
        )

    def upsert_candidate_subject(
        self,
        subject_id: str,
        source_type: str,
        source_ref: str,
        source_subject_id: str | None = None,
        source_label: str | None = None,
    ) -> bool:
        existing = self.connection.execute(
            "SELECT douban_subject_id FROM candidate_subject_queue WHERE douban_subject_id = %s",
            (subject_id,),
        ).fetchone()
        now = datetime.now(timezone.utc)
        self.connection.execute(
            """
            INSERT INTO candidate_subject_queue (
                douban_subject_id, source_type, source_ref, source_subject_id,
                source_label, status, error, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, 'pending', NULL, %s, %s)
            ON CONFLICT(douban_subject_id) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (subject_id, source_type, source_ref, source_subject_id, source_label, now, now),
        )
        return existing is None

    def find_pending_candidate_subjects(self, limit: int | None = None) -> list[CandidateSubjectQueueItem]:
        sql = """
            SELECT douban_subject_id, source_type, source_ref, source_subject_id, source_label, status
            FROM candidate_subject_queue
            WHERE status = 'pending'
            ORDER BY created_at, douban_subject_id
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT %s"
            params = (limit,)
        rows = self.connection.execute(sql, params).fetchall()
        return [
            CandidateSubjectQueueItem(
                douban_subject_id=str(row["douban_subject_id"]),
                source_type=str(row["source_type"]),
                source_ref=str(row["source_ref"]),
                source_subject_id=str(row["source_subject_id"]) if row["source_subject_id"] is not None else None,
                source_label=str(row["source_label"]) if row["source_label"] is not None else None,
                status=str(row["status"]),
            )
            for row in rows
        ]

    def mark_candidate_subject_status(self, subject_id: str, status: str, error: str | None = None) -> None:
        self.connection.execute(
            """
            UPDATE candidate_subject_queue
            SET status = %s, error = %s, updated_at = %s
            WHERE douban_subject_id = %s
            """,
            (status, error, datetime.now(timezone.utc), subject_id),
        )

    def upsert_candidate_pool_entry(self, movie_id: str, source_type: str, source_ref: str) -> bool:
        existing = self.connection.execute(
            """
            SELECT id FROM candidate_pool
            WHERE movie_id = %s AND source_type = %s AND source_ref = %s
            """,
            (movie_id, source_type, source_ref),
        ).fetchone()
        pool_id = str(existing["id"]) if existing is not None else str(uuid4())
        now = datetime.now(timezone.utc)
        self.connection.execute(
            """
            INSERT INTO candidate_pool (
                id, movie_id, source_type, source_ref, active, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, TRUE, %s, %s)
            ON CONFLICT(movie_id, source_type, source_ref) DO UPDATE SET
                active = TRUE,
                updated_at = excluded.updated_at
            """,
            (pool_id, movie_id, source_type, source_ref, now, now),
        )
        return existing is None


def _jsonb(value):
    from psycopg.types.json import Jsonb

    return Jsonb(value)
