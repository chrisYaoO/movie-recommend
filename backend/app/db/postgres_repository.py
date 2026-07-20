from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from backend.app.db.repository import (
    CandidateSubjectQueueItem,
    PersistedMovie,
    PersistedViewingHistory,
    PersistViewingHistoryResult,
    SheetSyncTask,
    ViewingHistoryRow,
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
                    aka_titles JSONB NOT NULL DEFAULT '[]'::jsonb,
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
            self.connection.execute("ALTER TABLE movies ADD COLUMN IF NOT EXISTS aka_titles JSONB NOT NULL DEFAULT '[]'::jsonb")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS viewing_history (
                    id UUID PRIMARY KEY,
                    movie_id UUID REFERENCES movies(id),
                    douban_subject_id TEXT NOT NULL,
                    watched_date DATE,
                    user_rating NUMERIC NOT NULL,
                    quality TEXT,
                    comment TEXT,
                    source_row_checksum TEXT NOT NULL,
                    source_sheet_name TEXT NOT NULL,
                    source_row_number INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            self.connection.execute("ALTER TABLE viewing_history ALTER COLUMN movie_id DROP NOT NULL")
            self.connection.execute("ALTER TABLE viewing_history ADD COLUMN IF NOT EXISTS douban_subject_id TEXT")
            self.connection.execute("ALTER TABLE viewing_history ADD COLUMN IF NOT EXISTS source_row_checksum TEXT")
            self.connection.execute("ALTER TABLE viewing_history ADD COLUMN IF NOT EXISTS source_sheet_name TEXT")
            self.connection.execute("ALTER TABLE viewing_history ADD COLUMN IF NOT EXISTS source_row_number INTEGER")
            self.connection.execute("ALTER TABLE viewing_history ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
            self.connection.execute("ALTER TABLE viewing_history DROP COLUMN IF EXISTS source_row_hash")
            self.connection.execute("ALTER TABLE viewing_history DROP COLUMN IF EXISTS source_file")
            self.connection.execute(
                """
                UPDATE viewing_history vh
                SET douban_subject_id = m.douban_subject_id
                FROM movies m
                WHERE vh.movie_id = m.id
                  AND vh.douban_subject_id IS NULL
                """
            )
            self.connection.execute("ALTER TABLE viewing_history ALTER COLUMN douban_subject_id SET NOT NULL")
            self.connection.execute("ALTER TABLE viewing_history ALTER COLUMN source_row_checksum SET NOT NULL")
            self.connection.execute("ALTER TABLE viewing_history ALTER COLUMN source_sheet_name SET NOT NULL")
            self.connection.execute("ALTER TABLE viewing_history ALTER COLUMN source_row_number SET NOT NULL")
            self.connection.execute("DROP INDEX IF EXISTS idx_viewing_history_source_row")
            self.connection.execute(
                """
                CREATE INDEX idx_viewing_history_source_row
                ON viewing_history(source_sheet_name, source_row_number)
                """
            )
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS sheet_sync_outbox (
                       history_id UUID PRIMARY KEY REFERENCES viewing_history(id),
                       operation TEXT NOT NULL CHECK(operation IN ('upsert', 'delete')),
                       attempts INTEGER NOT NULL DEFAULT 0,
                       last_error TEXT,
                       updated_at TIMESTAMPTZ NOT NULL
                   )"""
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
                    source_label TEXT,
                    active BOOLEAN NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(movie_id, source_type, source_ref)
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history_recommendation_discovery (
                    douban_subject_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            initialize_interaction_schema(self.connection)

    def persist_confirmed_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        detail: DoubanMovieDetail,
    ) -> PersistViewingHistoryResult:
        if not confirmed.source_row_checksum:
            raise ValueError("source_row_checksum is required when raw viewing history is not persisted")
        if confirmed.douban_subject_id != detail.subject_id:
            raise ValueError("confirmed subject id does not match detail subject id")

        with self.connection.transaction():
            movie = self.upsert_movie_detail(detail)
            history = self.upsert_viewing_history(confirmed, movie.id)
        return PersistViewingHistoryResult(movie=movie, history=history)

    def find_movie_by_subject_id(self, subject_id: str) -> PersistedMovie | None:
        row = self.connection.execute(
            "SELECT id, douban_subject_id, title, year, directors, poster_url FROM movies WHERE douban_subject_id = %s",
            (subject_id,),
        ).fetchone()
        if row is None:
            return None
        return PersistedMovie(
            id=str(row["id"]),
            douban_subject_id=str(row["douban_subject_id"]),
            title=str(row["title"]),
            year=int(row["year"]) if row["year"] is not None else None,
            directors=tuple(str(item) for item in (row["directors"] or [])),
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
            sql += " LIMIT %s"
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
            sql += " LIMIT %s"
            params = (limit,)
        rows = self.connection.execute(sql, params).fetchall()
        return [str(row["douban_subject_id"]) for row in rows]

    def backfill_viewing_history_movie_id(self, douban_subject_id: str, movie_id: str) -> int:
        cursor = self.connection.execute(
            """
            UPDATE viewing_history
            SET movie_id = %s, updated_at = %s
            WHERE douban_subject_id = %s
              AND (movie_id IS NULL OR movie_id <> %s)
            """,
            (movie_id, datetime.now(timezone.utc), douban_subject_id, movie_id),
        )
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
            sql += " LIMIT %s"
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
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
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
        ).fetchone()
        return int(row["count"])

    def mark_history_recommendation_discovery_status(
        self,
        subject_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.connection.execute(
            """
            INSERT INTO history_recommendation_discovery (
                douban_subject_id, status, error, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(douban_subject_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (subject_id, status, error, now, now),
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
                id, douban_subject_id, douban_url, title, aka_titles, year,
                directors, actors, genres, countries,
                douban_rating, douban_vote_count, summary, poster_url,
                raw_douban_json, metadata_status, created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT(douban_subject_id) DO UPDATE SET
                douban_url = excluded.douban_url,
                title = excluded.title,
                aka_titles = excluded.aka_titles,
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
                _jsonb(detail.aka_titles),
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

        history_id = confirmed.history_id or str(uuid4())
        now = datetime.now(timezone.utc)

        self.connection.execute(
            """
            INSERT INTO viewing_history (
                id, movie_id, douban_subject_id, watched_date, user_rating, quality, comment,
                source_row_checksum, source_sheet_name, source_row_number, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                confirmed.watched_date,
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
        with self.connection.transaction():
            history = self.upsert_viewing_history(confirmed, movie_id)
            self._enqueue_sheet_sync(history.id, "upsert")
        return history

    def update_viewing_history_and_enqueue(
        self,
        history_id: str,
        watched_date,
        user_rating: float,
        quality: str | None,
        comment: str | None,
        source_row_checksum: str,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self.connection.transaction():
            cursor = self.connection.execute(
                """UPDATE viewing_history
                   SET watched_date = %s, user_rating = %s, quality = %s, comment = %s,
                       source_row_checksum = %s, updated_at = %s
                   WHERE id = %s AND deleted_at IS NULL""",
                (watched_date, user_rating, quality, comment, source_row_checksum, now, history_id),
            )
            if not cursor.rowcount:
                return False
            self._enqueue_sheet_sync(history_id, "upsert", now)
        return True

    def soft_delete_viewing_history_and_enqueue(self, history_id: str) -> bool:
        now = datetime.now(timezone.utc)
        with self.connection.transaction():
            cursor = self.connection.execute(
                """UPDATE viewing_history
                   SET deleted_at = COALESCE(deleted_at, %s), updated_at = %s WHERE id = %s""",
                (now, now, history_id),
            )
            if not cursor.rowcount:
                return False
            self._enqueue_sheet_sync(history_id, "delete", now)
        return True

    def find_pending_sheet_sync_tasks(self, limit: int = 50) -> list[SheetSyncTask]:
        rows = self.connection.execute(
            """SELECT history_id, operation, attempts, last_error, updated_at
               FROM sheet_sync_outbox ORDER BY updated_at, history_id LIMIT %s""",
            (limit,),
        ).fetchall()
        return [
            SheetSyncTask(
                history_id=str(row["history_id"]), operation=str(row["operation"]),
                attempts=int(row["attempts"]), last_error=row["last_error"], updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def find_viewing_history(self, history_id: str, include_deleted: bool = False) -> ViewingHistoryRow | None:
        where = "vh.id = %s" if include_deleted else "vh.id = %s AND vh.deleted_at IS NULL"
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
                EXTRACT(YEAR FROM vh.watched_date)::INTEGER,
                CASE WHEN vh.source_sheet_name ~ '^[0-9]{4}$' THEN vh.source_sheet_name::INTEGER END
            ) = %s"""
            params.append(year)
        direction = "DESC" if descending else "ASC"
        rows = self.connection.execute(
            self._history_select()
            + f" WHERE {where} ORDER BY (vh.watched_date IS NULL), vh.watched_date {direction}, vh.created_at {direction} LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
        return [self._history_row(row) for row in rows]

    def count_active_viewing_history(self, year: int | None = None) -> int:
        where = "deleted_at IS NULL"
        params: tuple[int, ...] = ()
        if year is not None:
            where += """ AND COALESCE(
                EXTRACT(YEAR FROM watched_date)::INTEGER,
                CASE WHEN source_sheet_name ~ '^[0-9]{4}$' THEN source_sheet_name::INTEGER END
            ) = %s"""
            params = (year,)
        row = self.connection.execute(
            f"SELECT COUNT(*) AS count FROM viewing_history WHERE {where}",
            params,
        ).fetchone()
        return int(row["count"])

    def find_active_viewing_history_years(self) -> list[int]:
        rows = self.connection.execute(
            """SELECT DISTINCT watched_year AS year
               FROM (
                   SELECT COALESCE(
                       EXTRACT(YEAR FROM watched_date)::INTEGER,
                       CASE WHEN source_sheet_name ~ '^[0-9]{4}$' THEN source_sheet_name::INTEGER END
                   ) AS watched_year
                   FROM viewing_history
                   WHERE deleted_at IS NULL
               ) history_years
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
        with self.connection.transaction():
            if sheet_name is not None and row_number is not None:
                self.connection.execute(
                    "UPDATE viewing_history SET source_sheet_name = %s, source_row_number = %s, updated_at = %s WHERE id = %s",
                    (sheet_name, row_number, datetime.now(timezone.utc), history_id),
                )
            self.connection.execute(
                "DELETE FROM sheet_sync_outbox WHERE history_id = %s AND updated_at = %s",
                (history_id, expected_updated_at),
            )

    def fail_sheet_sync(self, history_id: str, expected_updated_at: datetime | str, error: str) -> None:
        self.connection.execute(
            """UPDATE sheet_sync_outbox SET attempts = attempts + 1, last_error = %s, updated_at = %s
               WHERE history_id = %s AND updated_at = %s""",
            (error[:500], datetime.now(timezone.utc), history_id, expected_updated_at),
        )

    def retry_sheet_sync(self, history_id: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE sheet_sync_outbox SET attempts = 0, last_error = NULL, updated_at = %s WHERE history_id = %s",
            (datetime.now(timezone.utc), history_id),
        )
        return bool(cursor.rowcount)

    def sheet_sync_health(self) -> dict[str, int | str | None]:
        row = self.connection.execute(
            """SELECT COUNT(*) AS pending_count,
                      COUNT(*) FILTER (WHERE attempts > 0) AS failed_count,
                      MAX(last_error) AS last_error
               FROM sheet_sync_outbox"""
        ).fetchone()
        return {
            "pending_count": int(row["pending_count"]),
            "failed_count": int(row["failed_count"]),
            "last_error": row["last_error"],
        }

    @staticmethod
    def _history_select() -> str:
        return """
            SELECT vh.id, vh.movie_id, vh.douban_subject_id, COALESCE(m.title, '') AS title,
                   m.year, COALESCE(m.directors, '[]'::jsonb) AS directors, m.poster_url,
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
            directors=tuple(str(value) for value in row["directors"]),
            poster_url=str(row["poster_url"]) if row["poster_url"] is not None else None,
            watched_date=row["watched_date"],
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

    def _enqueue_sheet_sync(self, history_id: str, operation: str, now=None) -> None:
        self.connection.execute(
            """INSERT INTO sheet_sync_outbox(history_id, operation, attempts, last_error, updated_at)
               VALUES (%s, %s, 0, NULL, %s)
               ON CONFLICT(history_id) DO UPDATE SET
                   operation = excluded.operation, attempts = 0, last_error = NULL, updated_at = excluded.updated_at""",
            (history_id, operation, now or datetime.now(timezone.utc)),
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
        return self.find_candidate_subjects_by_status("pending", limit=limit)

    def find_candidate_subjects_by_status(
        self,
        status: str,
        limit: int | None = None,
    ) -> list[CandidateSubjectQueueItem]:
        sql = """
            SELECT douban_subject_id, source_type, source_ref, source_subject_id, source_label, status
            FROM candidate_subject_queue
            WHERE status = %s
            ORDER BY created_at, douban_subject_id
        """
        params: tuple[str, ...] | tuple[str, int] = (status,)
        if limit is not None:
            sql += " LIMIT %s"
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
        sql = """
            SELECT douban_subject_id, source_type, source_ref, source_subject_id, source_label, status
            FROM candidate_subject_queue
            WHERE status = ANY(%s)
            ORDER BY created_at, douban_subject_id
        """
        params: tuple[object, ...] = (list(statuses),)
        if limit is not None:
            sql += " LIMIT %s"
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
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM candidate_subject_queue WHERE status = %s",
            (status,),
        ).fetchone()
        return int(row["count"])

    def mark_candidate_subject_status(self, subject_id: str, status: str, error: str | None = None) -> None:
        self.connection.execute(
            """
            UPDATE candidate_subject_queue
            SET status = %s, error = %s, updated_at = %s
            WHERE douban_subject_id = %s
            """,
            (status, error, datetime.now(timezone.utc), subject_id),
        )

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
            WHERE movie_id = %s AND source_type = %s AND source_ref = %s
            """,
            (movie_id, source_type, source_ref),
        ).fetchone()
        pool_id = str(existing["id"]) if existing is not None else str(uuid4())
        now = datetime.now(timezone.utc)
        self.connection.execute(
            """
            INSERT INTO candidate_pool (
                id, movie_id, source_type, source_ref, source_label, active, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
            ON CONFLICT(movie_id, source_type, source_ref) DO UPDATE SET
                source_label = COALESCE(excluded.source_label, candidate_pool.source_label),
                active = TRUE,
                updated_at = excluded.updated_at
            """,
            (pool_id, movie_id, source_type, source_ref, source_label, now, now),
        )
        return existing is None

    def backfill_candidate_source_labels_from_movies(self) -> int:
        now = datetime.now(timezone.utc)
        queue_cursor = self.connection.execute(
            """
            UPDATE candidate_subject_queue queue
            SET source_label = 'recommended from ' || source_movie.title,
                updated_at = %s
            FROM movies source_movie
            WHERE queue.source_ref LIKE 'recommended_from:%'
              AND (queue.source_label IS NULL OR queue.source_label = '')
              AND source_movie.douban_subject_id = split_part(queue.source_ref, ':', 2)
            """,
            (now,),
        )
        pool_cursor = self.connection.execute(
            """
            UPDATE candidate_pool pool
            SET source_label = 'recommended from ' || source_movie.title,
                updated_at = %s
            FROM movies source_movie
            WHERE pool.source_ref LIKE 'recommended_from:%'
              AND (pool.source_label IS NULL OR pool.source_label = '')
              AND source_movie.douban_subject_id = split_part(pool.source_ref, ':', 2)
            """,
            (now,),
        )
        return int(queue_cursor.rowcount or 0) + int(pool_cursor.rowcount or 0)


def _jsonb(value):
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def initialize_interaction_schema(connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_sessions (
            id UUID PRIMARY KEY,
            strategy TEXT NOT NULL,
            context_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_items (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES recommendation_sessions(id),
            movie_id UUID NOT NULL REFERENCES movies(id),
            rank INTEGER NOT NULL,
            slot_type TEXT NOT NULL,
            score NUMERIC NOT NULL,
            score_components JSONB NOT NULL DEFAULT '{}'::jsonb,
            source_ref TEXT,
            source_label TEXT,
            processing_status TEXT,
            processed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            UNIQUE(session_id, rank)
        )
        """
    )
    connection.execute("ALTER TABLE candidate_pool ADD COLUMN IF NOT EXISTS source_label TEXT")
    connection.execute("ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS source_ref TEXT")
    connection.execute("ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS source_label TEXT")
    connection.execute("ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS processing_status TEXT")
    connection.execute("ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES recommendation_sessions(id),
            item_id UUID NOT NULL REFERENCES recommendation_items(id),
            movie_id UUID NOT NULL REFERENCES movies(id),
            feedback_type TEXT NOT NULL,
            feedback_value NUMERIC NOT NULL,
            comment TEXT,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS wishlist (
            id UUID PRIMARY KEY,
            movie_id UUID NOT NULL REFERENCES movies(id),
            source_session_id UUID NOT NULL REFERENCES recommendation_sessions(id),
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ
        )
        """
    )


