from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail
from backend.app.db.repository import (
    CandidateSubjectQueueItem,
    PersistedMovie,
    PersistedViewingHistory,
    PersistViewingHistoryResult,
)


class SQLiteViewingHistoryRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS movies (
                id TEXT PRIMARY KEY,
                douban_subject_id TEXT NOT NULL UNIQUE,
                douban_url TEXT,
                title TEXT NOT NULL,
                aka_titles_json TEXT NOT NULL DEFAULT '[]',
                year INTEGER,
                directors_json TEXT NOT NULL,
                actors_json TEXT NOT NULL,
                genres_json TEXT NOT NULL,
                countries_json TEXT NOT NULL,
                douban_rating REAL,
                douban_vote_count INTEGER,
                summary TEXT,
                poster_url TEXT,
                raw_douban_json TEXT NOT NULL,
                metadata_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS viewing_history (
                id TEXT PRIMARY KEY,
                movie_id TEXT REFERENCES movies(id),
                douban_subject_id TEXT NOT NULL,
                watched_date TEXT,
                user_rating REAL NOT NULL,
                quality TEXT,
                comment TEXT,
                source_row_checksum TEXT NOT NULL,
                source_sheet_name TEXT NOT NULL,
                source_row_number INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidate_subject_queue (
                douban_subject_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_subject_id TEXT,
                source_label TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidate_pool (
                id TEXT PRIMARY KEY,
                movie_id TEXT NOT NULL REFERENCES movies(id),
                source_type TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_label TEXT,
                active INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(movie_id, source_type, source_ref)
            );

            CREATE TABLE IF NOT EXISTS history_recommendation_discovery (
                douban_subject_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._add_column_if_missing("movies", "aka_titles_json", "TEXT NOT NULL DEFAULT '[]'")
        self._add_column_if_missing("viewing_history", "douban_subject_id", "TEXT")
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_viewing_history_source_row
            ON viewing_history(source_sheet_name, source_row_number)
            """
        )
        self.connection.commit()

    def _add_column_if_missing(self, table_name: str, column_name: str, column_definition: str) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            self.connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

    def persist_confirmed_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        detail: DoubanMovieDetail,
    ) -> PersistViewingHistoryResult:
        if not confirmed.source_row_checksum:
            raise ValueError("source_row_checksum is required when raw viewing history is not persisted")
        if confirmed.douban_subject_id != detail.subject_id:
            raise ValueError("confirmed subject id does not match detail subject id")

        with self.connection:
            movie = self.upsert_movie_detail(detail)
            history = self.upsert_viewing_history(confirmed, movie.id)
        return PersistViewingHistoryResult(movie=movie, history=history)

    def find_movie_by_subject_id(self, subject_id: str) -> PersistedMovie | None:
        row = self.connection.execute(
            "SELECT id, douban_subject_id, title, year, directors_json, poster_url FROM movies WHERE douban_subject_id = ?",
            (subject_id,),
        ).fetchone()
        if row is None:
            return None
        return PersistedMovie(
            id=str(row["id"]),
            douban_subject_id=str(row["douban_subject_id"]),
            title=str(row["title"]),
            year=int(row["year"]) if row["year"] is not None else None,
            directors=tuple(str(item) for item in json.loads(row["directors_json"] or "[]")),
            poster_url=str(row["poster_url"]) if row["poster_url"] is not None else None,
        )

    def find_watched_movies(self, limit: int | None = None) -> list[PersistedMovie]:
        sql = """
            SELECT m.id, m.douban_subject_id, m.title, MIN(vh.created_at) AS first_watched_created_at
            FROM viewing_history vh
            JOIN movies m ON m.douban_subject_id = vh.douban_subject_id
            GROUP BY m.id, m.douban_subject_id, m.title
            ORDER BY first_watched_created_at, m.douban_subject_id
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self.connection.execute(sql, params).fetchall()
        return [
            PersistedMovie(
                id=str(row["id"]),
                douban_subject_id=str(row["douban_subject_id"]),
                title=str(row["title"]),
            )
            for row in rows
        ]

    def find_history_subject_ids_missing_movies(self, limit: int | None = None) -> list[str]:
        sql = """
            SELECT vh.douban_subject_id, MIN(vh.created_at) AS first_watched_created_at
            FROM viewing_history vh
            LEFT JOIN movies m ON m.douban_subject_id = vh.douban_subject_id
            WHERE m.id IS NULL
            GROUP BY vh.douban_subject_id
            ORDER BY first_watched_created_at, vh.douban_subject_id
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self.connection.execute(sql, params).fetchall()
        return [str(row["douban_subject_id"]) for row in rows]

    def backfill_viewing_history_movie_id(self, douban_subject_id: str, movie_id: str) -> int:
        cursor = self.connection.execute(
            """
            UPDATE viewing_history
            SET movie_id = ?, updated_at = ?
            WHERE douban_subject_id = ?
              AND (movie_id IS NULL OR movie_id <> ?)
            """,
            (movie_id, _utc_now(), douban_subject_id, movie_id),
        )
        self.connection.commit()
        return int(cursor.rowcount or 0)

    def find_unprocessed_watched_movies_for_history_recommendations(
        self,
        limit: int | None = None,
    ) -> list[PersistedMovie]:
        sql = """
            SELECT watched.id, watched.douban_subject_id, watched.title
            FROM (
                SELECT m.id, m.douban_subject_id, m.title, MIN(vh.created_at) AS first_watched_created_at
                FROM viewing_history vh
                JOIN movies m ON m.douban_subject_id = vh.douban_subject_id
                GROUP BY m.id, m.douban_subject_id, m.title
            ) watched
            LEFT JOIN history_recommendation_discovery discovery
                ON discovery.douban_subject_id = watched.douban_subject_id
                AND discovery.status = 'completed'
            WHERE discovery.douban_subject_id IS NULL
            ORDER BY watched.first_watched_created_at, watched.douban_subject_id
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self.connection.execute(sql, params).fetchall()
        return [
            PersistedMovie(
                id=str(row["id"]),
                douban_subject_id=str(row["douban_subject_id"]),
                title=str(row["title"]),
            )
            for row in rows
        ]

    def count_unprocessed_watched_movies_for_history_recommendations(self) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT m.douban_subject_id
                    FROM viewing_history vh
                    JOIN movies m ON m.douban_subject_id = vh.douban_subject_id
                    GROUP BY m.id, m.douban_subject_id
                ) watched
                LEFT JOIN history_recommendation_discovery discovery
                    ON discovery.douban_subject_id = watched.douban_subject_id
                    AND discovery.status = 'completed'
                WHERE discovery.douban_subject_id IS NULL
                """
            ).fetchone()[0]
        )

    def mark_history_recommendation_discovery_status(
        self,
        subject_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        self.connection.execute(
            """
            INSERT INTO history_recommendation_discovery (
                douban_subject_id, status, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(douban_subject_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (subject_id, status, error, now, now),
        )
        self.connection.commit()

    def upsert_movie_detail(self, detail: DoubanMovieDetail) -> PersistedMovie:
        existing = self.connection.execute(
            "SELECT id FROM movies WHERE douban_subject_id = ?",
            (detail.subject_id,),
        ).fetchone()
        movie_id = str(existing["id"]) if existing is not None else str(uuid4())
        now = _utc_now()

        self.connection.execute(
            """
            INSERT INTO movies (
                id, douban_subject_id, douban_url, title, aka_titles_json, year,
                directors_json, actors_json, genres_json, countries_json,
                douban_rating, douban_vote_count, summary, poster_url,
                raw_douban_json, metadata_status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(douban_subject_id) DO UPDATE SET
                douban_url = excluded.douban_url,
                title = excluded.title,
                aka_titles_json = excluded.aka_titles_json,
                year = excluded.year,
                directors_json = excluded.directors_json,
                actors_json = excluded.actors_json,
                genres_json = excluded.genres_json,
                countries_json = excluded.countries_json,
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
                _json(detail.aka_titles),
                detail.year,
                _json(detail.directors),
                _json(detail.actors),
                _json(detail.genres),
                _json(detail.countries),
                detail.douban_rating,
                detail.douban_vote_count,
                detail.summary,
                detail.poster_url,
                _json(asdict(detail)),
                "enriched",
                now,
                now,
            ),
        )
        self.connection.commit()
        return PersistedMovie(
            id=movie_id,
            douban_subject_id=detail.subject_id,
            title=detail.title,
            year=detail.year,
            directors=detail.directors,
            poster_url=detail.poster_url,
        )

    def upsert_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        movie_id: str | None = None,
    ) -> PersistedViewingHistory:
        if not confirmed.source_row_checksum:
            raise ValueError("source_row_checksum is required when raw viewing history is not persisted")

        existing = self.connection.execute(
            "SELECT id FROM viewing_history WHERE source_sheet_name = ? AND source_row_number = ?",
            (confirmed.source_sheet_name, confirmed.source_row_number),
        ).fetchone()
        history_id = str(existing["id"]) if existing is not None else str(uuid4())
        now = _utc_now()

        self.connection.execute(
            """
            INSERT INTO viewing_history (
                id, movie_id, douban_subject_id, watched_date, user_rating, quality, comment,
                source_row_checksum, source_sheet_name, source_row_number, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_sheet_name, source_row_number) DO UPDATE SET
                movie_id = excluded.movie_id,
                douban_subject_id = excluded.douban_subject_id,
                watched_date = excluded.watched_date,
                user_rating = excluded.user_rating,
                quality = excluded.quality,
                comment = excluded.comment,
                source_row_checksum = excluded.source_row_checksum,
                updated_at = excluded.updated_at
            """,
            (
                history_id,
                movie_id,
                confirmed.douban_subject_id,
                _date_to_text(confirmed.watched_date),
                confirmed.user_rating,
                confirmed.quality,
                confirmed.comment,
                confirmed.source_row_checksum,
                confirmed.source_sheet_name,
                confirmed.source_row_number,
                now,
                now,
            ),
        )
        self.connection.commit()
        return PersistedViewingHistory(
            id=history_id,
            douban_subject_id=confirmed.douban_subject_id,
            movie_id=movie_id,
            source_row_checksum=confirmed.source_row_checksum,
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
            "SELECT douban_subject_id FROM candidate_subject_queue WHERE douban_subject_id = ?",
            (subject_id,),
        ).fetchone()
        now = _utc_now()
        self.connection.execute(
            """
            INSERT INTO candidate_subject_queue (
                douban_subject_id, source_type, source_ref, source_subject_id,
                source_label, status, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
            ON CONFLICT(douban_subject_id) DO UPDATE SET
                updated_at = excluded.updated_at
            """,
            (subject_id, source_type, source_ref, source_subject_id, source_label, now, now),
        )
        self.connection.commit()
        return existing is None

    def find_pending_candidate_subjects(self, limit: int | None = None) -> list[CandidateSubjectQueueItem]:
        return self.find_candidate_subjects_by_status("pending", limit=limit)

    def find_candidate_subjects_by_status(
        self,
        status: str,
        limit: int | None = None,
    ) -> list[CandidateSubjectQueueItem]:
        sql = """
            SELECT douban_subject_id, source_type, source_ref, source_subject_id, source_label, status
            FROM candidate_subject_queue
            WHERE status = ?
            ORDER BY created_at, douban_subject_id
        """
        params: tuple[str, ...] | tuple[str, int] = (status,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (status, limit)
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

    def count_candidate_subjects_by_status(self, status: str) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM candidate_subject_queue WHERE status = ?",
                (status,),
            ).fetchone()[0]
        )

    def mark_candidate_subject_status(self, subject_id: str, status: str, error: str | None = None) -> None:
        self.connection.execute(
            """
            UPDATE candidate_subject_queue
            SET status = ?, error = ?, updated_at = ?
            WHERE douban_subject_id = ?
            """,
            (status, error, _utc_now(), subject_id),
        )
        self.connection.commit()

    def upsert_candidate_pool_entry(
        self,
        movie_id: str,
        source_type: str,
        source_ref: str,
        source_label: str | None = None,
    ) -> bool:
        existing = self.connection.execute(
            """
            SELECT id FROM candidate_pool
            WHERE movie_id = ? AND source_type = ? AND source_ref = ?
            """,
            (movie_id, source_type, source_ref),
        ).fetchone()
        pool_id = str(existing["id"]) if existing is not None else str(uuid4())
        now = _utc_now()
        self.connection.execute(
            """
            INSERT INTO candidate_pool (
                id, movie_id, source_type, source_ref, source_label, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(movie_id, source_type, source_ref) DO UPDATE SET
                source_label = COALESCE(excluded.source_label, candidate_pool.source_label),
                active = 1,
                updated_at = excluded.updated_at
            """,
            (pool_id, movie_id, source_type, source_ref, source_label, now, now),
        )
        self.connection.commit()
        return existing is None

    def backfill_candidate_source_labels_from_movies(self) -> int:
        queue_cursor = self.connection.execute(
            """
            UPDATE candidate_subject_queue
            SET source_label = (
                    SELECT 'recommended from ' || movies.title
                    FROM movies
                    WHERE movies.douban_subject_id = substr(candidate_subject_queue.source_ref, length('recommended_from:') + 1)
                ),
                updated_at = ?
            WHERE source_ref LIKE 'recommended_from:%'
              AND (source_label IS NULL OR source_label = '')
              AND EXISTS (
                    SELECT 1
                    FROM movies
                    WHERE movies.douban_subject_id = substr(candidate_subject_queue.source_ref, length('recommended_from:') + 1)
              )
            """,
            (_utc_now(),),
        )
        pool_cursor = self.connection.execute(
            """
            UPDATE candidate_pool
            SET source_label = (
                    SELECT 'recommended from ' || movies.title
                    FROM movies
                    WHERE movies.douban_subject_id = substr(candidate_pool.source_ref, length('recommended_from:') + 1)
                ),
                updated_at = ?
            WHERE source_ref LIKE 'recommended_from:%'
              AND (source_label IS NULL OR source_label = '')
              AND EXISTS (
                    SELECT 1
                    FROM movies
                    WHERE movies.douban_subject_id = substr(candidate_pool.source_ref, length('recommended_from:') + 1)
              )
            """,
            (_utc_now(),),
        )
        self.connection.commit()
        return int(queue_cursor.rowcount or 0) + int(pool_cursor.rowcount or 0)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _date_to_text(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


