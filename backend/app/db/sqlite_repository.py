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
    SheetSyncTask,
    ViewingHistoryRow,
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

            CREATE TABLE IF NOT EXISTS sheet_sync_outbox (
                history_id TEXT PRIMARY KEY REFERENCES viewing_history(id),
                operation TEXT NOT NULL CHECK(operation IN ('upsert', 'delete')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
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
        self._add_column_if_missing("viewing_history", "deleted_at", "TEXT")
        self.connection.execute("DROP INDEX IF EXISTS idx_viewing_history_source_row")
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_viewing_history_source_row
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
            WHERE vh.deleted_at IS NULL
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
            WHERE m.id IS NULL AND vh.deleted_at IS NULL
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
                WHERE vh.deleted_at IS NULL
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
                    WHERE vh.deleted_at IS NULL
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
        history = self._write_viewing_history(confirmed, movie_id)
        self.connection.commit()
        return history

    def _write_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        movie_id: str | None = None,
    ) -> PersistedViewingHistory:
        if not confirmed.source_row_checksum:
            raise ValueError("source_row_checksum is required when raw viewing history is not persisted")

        history_id = confirmed.history_id or str(uuid4())
        now = _utc_now()

        self.connection.execute(
            """
            INSERT INTO viewing_history (
                id, movie_id, douban_subject_id, watched_date, user_rating, quality, comment,
                source_row_checksum, source_sheet_name, source_row_number, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                movie_id = excluded.movie_id,
                douban_subject_id = excluded.douban_subject_id,
                watched_date = excluded.watched_date,
                user_rating = excluded.user_rating,
                quality = excluded.quality,
                comment = excluded.comment,
                source_row_checksum = excluded.source_row_checksum,
                source_sheet_name = excluded.source_sheet_name,
                source_row_number = excluded.source_row_number,
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
        return PersistedViewingHistory(
            id=history_id,
            douban_subject_id=confirmed.douban_subject_id,
            movie_id=movie_id,
            source_row_checksum=confirmed.source_row_checksum,
        )

    def save_viewing_history_and_enqueue(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        movie_id: str | None = None,
    ) -> PersistedViewingHistory:
        with self.connection:
            history = self._write_viewing_history(confirmed, movie_id)
            self._enqueue_sheet_sync(history.id, "upsert")
        return history

    def update_viewing_history_and_enqueue(
        self,
        history_id: str,
        watched_date: date,
        user_rating: float,
        quality: str | None,
        comment: str | None,
        source_row_checksum: str,
    ) -> bool:
        now = _utc_now()
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE viewing_history
                   SET watched_date = ?, user_rating = ?, quality = ?, comment = ?,
                       source_row_checksum = ?, updated_at = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (_date_to_text(watched_date), user_rating, quality, comment, source_row_checksum, now, history_id),
            )
            if not cursor.rowcount:
                return False
            self._enqueue_sheet_sync(history_id, "upsert", now)
        return True

    def soft_delete_viewing_history_and_enqueue(self, history_id: str) -> bool:
        now = _utc_now()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE viewing_history SET deleted_at = COALESCE(deleted_at, ?), updated_at = ? WHERE id = ?",
                (now, now, history_id),
            )
            if not cursor.rowcount:
                return False
            self._enqueue_sheet_sync(history_id, "delete", now)
        return True

    def find_pending_sheet_sync_tasks(self, limit: int = 50) -> list[SheetSyncTask]:
        rows = self.connection.execute(
            """SELECT history_id, operation, attempts, last_error, updated_at
               FROM sheet_sync_outbox ORDER BY updated_at, history_id LIMIT ?""",
            (limit,),
        ).fetchall()
        return [SheetSyncTask(**dict(row)) for row in rows]

    def find_viewing_history(self, history_id: str, include_deleted: bool = False) -> ViewingHistoryRow | None:
        where = "vh.id = ?" if include_deleted else "vh.id = ? AND vh.deleted_at IS NULL"
        row = self.connection.execute(self._history_select() + f" WHERE {where}", (history_id,)).fetchone()
        return self._history_row(row) if row else None

    def find_active_viewing_history(
        self,
        limit: int = 50,
        offset: int = 0,
        year: int | None = None,
        descending: bool = True,
    ) -> list[ViewingHistoryRow]:
        where = "vh.deleted_at IS NULL"
        params: list[int] = []
        if year is not None:
            where += """ AND COALESCE(
                CAST(substr(vh.watched_date, 1, 4) AS INTEGER),
                CASE WHEN length(vh.source_sheet_name) = 4
                          AND vh.source_sheet_name GLOB '[0-9][0-9][0-9][0-9]'
                     THEN CAST(vh.source_sheet_name AS INTEGER) END
            ) = ?"""
            params.append(year)
        direction = "DESC" if descending else "ASC"
        rows = self.connection.execute(
            self._history_select()
            + f" WHERE {where} ORDER BY (vh.watched_date IS NULL), vh.watched_date {direction}, vh.created_at {direction} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [self._history_row(row) for row in rows]

    def count_active_viewing_history(self, year: int | None = None) -> int:
        where = "deleted_at IS NULL"
        params: tuple[int, ...] = ()
        if year is not None:
            where += """ AND COALESCE(
                CAST(substr(watched_date, 1, 4) AS INTEGER),
                CASE WHEN length(source_sheet_name) = 4
                          AND source_sheet_name GLOB '[0-9][0-9][0-9][0-9]'
                     THEN CAST(source_sheet_name AS INTEGER) END
            ) = ?"""
            params = (year,)
        return int(self.connection.execute(f"SELECT COUNT(*) FROM viewing_history WHERE {where}", params).fetchone()[0])

    def find_active_viewing_history_years(self) -> list[int]:
        rows = self.connection.execute(
            """SELECT DISTINCT watched_year AS year
               FROM (
                   SELECT COALESCE(
                       CAST(substr(watched_date, 1, 4) AS INTEGER),
                       CASE WHEN length(source_sheet_name) = 4
                                 AND source_sheet_name GLOB '[0-9][0-9][0-9][0-9]'
                            THEN CAST(source_sheet_name AS INTEGER) END
                   ) AS watched_year
                   FROM viewing_history
                   WHERE deleted_at IS NULL
               )
               WHERE watched_year IS NOT NULL
               ORDER BY watched_year DESC"""
        ).fetchall()
        return [int(row["year"]) for row in rows]

    def complete_sheet_sync(
        self,
        history_id: str,
        expected_updated_at: datetime | str,
        sheet_name: str | None = None,
        row_number: int | None = None,
    ) -> None:
        with self.connection:
            if sheet_name is not None and row_number is not None:
                self.connection.execute(
                    "UPDATE viewing_history SET source_sheet_name = ?, source_row_number = ?, updated_at = ? WHERE id = ?",
                    (sheet_name, row_number, _utc_now(), history_id),
                )
            self.connection.execute(
                "DELETE FROM sheet_sync_outbox WHERE history_id = ? AND updated_at = ?",
                (history_id, expected_updated_at),
            )

    def fail_sheet_sync(self, history_id: str, expected_updated_at: datetime | str, error: str) -> None:
        self.connection.execute(
            """UPDATE sheet_sync_outbox SET attempts = attempts + 1, last_error = ?, updated_at = ?
               WHERE history_id = ? AND updated_at = ?""",
            (error[:500], _utc_now(), history_id, expected_updated_at),
        )
        self.connection.commit()

    def retry_sheet_sync(self, history_id: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE sheet_sync_outbox SET attempts = 0, last_error = NULL, updated_at = ? WHERE history_id = ?",
            (_utc_now(), history_id),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def sheet_sync_health(self) -> dict[str, int | str | None]:
        row = self.connection.execute(
            """SELECT COUNT(*) AS pending_count,
                      SUM(CASE WHEN attempts > 0 THEN 1 ELSE 0 END) AS failed_count,
                      MAX(last_error) AS last_error
               FROM sheet_sync_outbox"""
        ).fetchone()
        return {
            "pending_count": int(row["pending_count"]),
            "failed_count": int(row["failed_count"] or 0),
            "last_error": row["last_error"],
        }

    @staticmethod
    def _history_select() -> str:
        return """
            SELECT vh.id, vh.movie_id, vh.douban_subject_id, COALESCE(m.title, '') AS title,
                   m.year, COALESCE(m.directors_json, '[]') AS directors, m.poster_url,
                   vh.watched_date, vh.user_rating, vh.quality, vh.comment,
                   vh.source_row_checksum, vh.source_sheet_name, vh.source_row_number, vh.deleted_at,
                   outbox.operation AS sync_operation, COALESCE(outbox.attempts, 0) AS sync_attempts,
                   outbox.last_error AS sync_error
            FROM viewing_history vh
            LEFT JOIN movies m ON m.id = vh.movie_id
            LEFT JOIN sheet_sync_outbox outbox ON outbox.history_id = vh.id
        """

    @staticmethod
    def _history_row(row) -> ViewingHistoryRow:
        return ViewingHistoryRow(
            id=str(row["id"]),
            movie_id=str(row["movie_id"]) if row["movie_id"] is not None else None,
            douban_subject_id=str(row["douban_subject_id"]),
            title=str(row["title"]),
            year=int(row["year"]) if row["year"] is not None else None,
            directors=tuple(str(value) for value in json.loads(row["directors"] or "[]")),
            poster_url=str(row["poster_url"]) if row["poster_url"] is not None else None,
            watched_date=date.fromisoformat(row["watched_date"]) if row["watched_date"] else None,
            user_rating=float(row["user_rating"]),
            quality=row["quality"],
            comment=row["comment"],
            source_row_checksum=str(row["source_row_checksum"]),
            source_sheet_name=str(row["source_sheet_name"]),
            source_row_number=int(row["source_row_number"]),
            deleted_at=row["deleted_at"],
            sync_operation=row["sync_operation"],
            sync_attempts=int(row["sync_attempts"]),
            sync_error=row["sync_error"],
        )

    def _enqueue_sheet_sync(self, history_id: str, operation: str, now: str | None = None) -> None:
        self.connection.execute(
            """INSERT INTO sheet_sync_outbox(history_id, operation, attempts, last_error, updated_at)
               VALUES (?, ?, 0, NULL, ?)
               ON CONFLICT(history_id) DO UPDATE SET
                   operation = excluded.operation, attempts = 0, last_error = NULL, updated_at = excluded.updated_at""",
            (history_id, operation, now or _utc_now()),
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

    def find_candidate_subjects_by_statuses(
        self,
        statuses: tuple[str, ...],
        limit: int | None = None,
    ) -> list[CandidateSubjectQueueItem]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        sql = f"""
            SELECT douban_subject_id, source_type, source_ref, source_subject_id, source_label, status
            FROM candidate_subject_queue
            WHERE status IN ({placeholders})
            ORDER BY created_at, douban_subject_id
        """
        params: tuple[object, ...] = tuple(statuses)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
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


