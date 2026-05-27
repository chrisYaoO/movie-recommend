import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from jobs.candidate_pool import (
    DOUBAN_RECOMMENDATION_SOURCE,
    DOUBAN_TOP250_SOURCE,
    discover_history_recommendations,
    discover_top250_subjects,
    parse_recommended_subject_ids,
    parse_top250_subject_ids,
    process_candidate_queue,
)


class CandidatePoolJobTest(unittest.TestCase):
    def test_parse_top250_subject_ids_deduplicates_page_links(self) -> None:
        html = """
        <a href="https://movie.douban.com/subject/1292052/">A</a>
        <a href="https://movie.douban.com/subject/1292052/">A duplicate</a>
        <a href="https://movie.douban.com/subject/1291546/">B</a>
        """

        self.assertEqual(["1292052", "1291546"], parse_top250_subject_ids(html))

    def test_discovers_top250_with_top_rank_source_ref(self) -> None:
        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                result = discover_top250_subjects(repository, _FakeTop250Client(["1292052", "1291546"]))
                rows = repository.connection.execute(
                    "SELECT douban_subject_id, source_type, source_ref, status FROM candidate_subject_queue ORDER BY source_ref"
                ).fetchall()

        self.assertEqual(2, result.discovered_count)
        self.assertEqual(2, result.inserted_count)
        self.assertEqual(
            [("1292052", DOUBAN_TOP250_SOURCE, "top1", "pending"), ("1291546", DOUBAN_TOP250_SOURCE, "top2", "pending")],
            [tuple(row) for row in rows],
        )

    def test_process_queue_enriches_movie_activates_pool_and_queues_one_layer_recommendations(self) -> None:
        html = _recommendations_html("电影", ("1292720", "1292064"))

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                repository.upsert_candidate_subject("1292052", DOUBAN_TOP250_SOURCE, "top1")
                adapter = _FakeDetailPageAdapter(
                    {"1292052": _detail("1292052", "肖申克的救赎 The Shawshank Redemption")},
                    {"1292052": html},
                )

                result = process_candidate_queue(repository, adapter)
                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                pool_count = repository.connection.execute("SELECT COUNT(*) FROM candidate_pool").fetchone()[0]
                queue_rows = repository.connection.execute(
                    """
                    SELECT douban_subject_id, source_type, source_ref, source_subject_id, source_label, status
                    FROM candidate_subject_queue
                    ORDER BY douban_subject_id
                    """
                ).fetchall()

        self.assertEqual(1, result.attempted_count)
        self.assertEqual(1, result.enriched_count)
        self.assertEqual(1, result.candidate_pool_inserted_count)
        self.assertEqual(2, result.recommendation_discovered_count)
        self.assertEqual(2, result.recommendation_inserted_count)
        self.assertEqual(1, movie_count)
        self.assertEqual(1, pool_count)
        self.assertIn(
            (
                "1292720",
                DOUBAN_RECOMMENDATION_SOURCE,
                "recommended_from:1292052",
                "1292052",
                "recommended from 肖申克的救赎 The Shawshank Redemption",
                "pending",
            ),
            [tuple(row) for row in queue_rows],
        )

    def test_process_queue_reuses_existing_movie_without_fetching_or_expanding_recommendations(self) -> None:
        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                existing = repository.upsert_movie_detail(_detail("1292052", "Existing Movie"))
                repository.upsert_candidate_subject("1292052", DOUBAN_TOP250_SOURCE, "top1")

                result = process_candidate_queue(repository, _FakeDetailPageAdapter({}, {}))
                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                pool = repository.connection.execute("SELECT movie_id, source_type, source_ref FROM candidate_pool").fetchone()

        self.assertEqual(1, result.existing_movie_count)
        self.assertEqual(0, result.enriched_count)
        self.assertEqual(0, result.recommendation_discovered_count)
        self.assertEqual(1, movie_count)
        self.assertEqual((existing.id, DOUBAN_TOP250_SOURCE, "top1"), tuple(pool))

    def test_process_queue_can_retry_failed_rows_without_processing_pending(self) -> None:
        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                repository.upsert_candidate_subject("failed-subject", DOUBAN_TOP250_SOURCE, "top1")
                repository.upsert_candidate_subject("pending-subject", DOUBAN_TOP250_SOURCE, "top2")
                repository.mark_candidate_subject_status("failed-subject", "failed", "temporary failure")
                adapter = _FakeDetailPageAdapter(
                    {"failed-subject": _detail("failed-subject", "Recovered Movie")},
                    {"failed-subject": ""},
                )

                result = process_candidate_queue(repository, adapter, queue_status="failed")
                queue_rows = repository.connection.execute(
                    """
                    SELECT douban_subject_id, status
                    FROM candidate_subject_queue
                    ORDER BY douban_subject_id
                    """
                ).fetchall()

        self.assertEqual(1, result.attempted_count)
        self.assertEqual(1, result.enriched_count)
        self.assertEqual([("failed-subject", "enriched"), ("pending-subject", "pending")], [tuple(row) for row in queue_rows])

    def test_process_queue_writes_status_progress(self) -> None:
        messages: list[str] = []
        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                repository.upsert_candidate_subject("1292052", DOUBAN_TOP250_SOURCE, "top1")
                adapter = _FakeDetailPageAdapter(
                    {"1292052": _detail("1292052", "The Shawshank Redemption")},
                    {"1292052": ""},
                )

                process_candidate_queue(repository, adapter, status_writer=messages.append)

        self.assertIn("[queue] status=pending, remaining=1, selected=1, limit=None", messages)
        self.assertTrue(any("subject=1292052" in message for message in messages))
        self.assertTrue(any("remaining_pending=0" in message for message in messages))
        self.assertTrue(any(message.startswith("[summary] attempted=1") for message in messages))

    def test_discovers_recommendations_from_watched_movies_into_queue_only(self) -> None:
        html = _recommendations_html("电影", ("1292720", "1292064"))

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                repository.persist_confirmed_viewing_history(
                    _confirmed("1292052"),
                    _detail("1292052", "Watched Movie"),
                )
                adapter = _FakeDetailPageAdapter(
                    {"1292052": _detail("1292052", "Watched Movie")},
                    {"1292052": html},
                )

                result = discover_history_recommendations(repository, adapter)
                queue_rows = repository.connection.execute(
                    """
                    SELECT douban_subject_id, source_type, source_ref, source_subject_id, source_label, status
                    FROM candidate_subject_queue
                    ORDER BY douban_subject_id
                    """
                ).fetchall()
                pool_count = repository.connection.execute("SELECT COUNT(*) FROM candidate_pool").fetchone()[0]

        self.assertEqual(1, result.watched_movie_count)
        self.assertEqual(1, result.attempted_count)
        self.assertEqual(2, result.recommendation_discovered_count)
        self.assertEqual(2, result.recommendation_inserted_count)
        self.assertEqual(0, pool_count)
        self.assertEqual(
            [
                (
                    "1292064",
                    DOUBAN_RECOMMENDATION_SOURCE,
                    "recommended_from:1292052",
                    "1292052",
                    "recommended from Watched Movie",
                    "pending",
                ),
                (
                    "1292720",
                    DOUBAN_RECOMMENDATION_SOURCE,
                    "recommended_from:1292052",
                    "1292052",
                    "recommended from Watched Movie",
                    "pending",
                ),
            ],
            [tuple(row) for row in queue_rows],
        )

    def test_discover_history_recommendations_writes_remaining_progress(self) -> None:
        html = _recommendations_html("电影", ("1292720",))
        messages: list[str] = []

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                repository.persist_confirmed_viewing_history(
                    _confirmed("1292052"),
                    _detail("1292052", "Watched Movie"),
                )
                adapter = _FakeDetailPageAdapter(
                    {"1292052": _detail("1292052", "Watched Movie")},
                    {"1292052": html},
                )

                discover_history_recommendations(repository, adapter, status_writer=messages.append)

        self.assertIn("[history-recommendation] remaining=1, selected=1, limit=None", messages)
        self.assertTrue(any("subject=1292052" in message for message in messages))
        self.assertTrue(any("remaining_watched=0" in message for message in messages))
        self.assertTrue(any(message.startswith("[summary] attempted=1") for message in messages))

    def test_discover_history_recommendations_resumes_unprocessed_watched_movies(self) -> None:
        html = _recommendations_html("电影", ("1292720",))

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                repository.persist_confirmed_viewing_history(
                    _confirmed("1292052", source_row_number=8),
                    _detail("1292052", "Completed Movie"),
                )
                repository.persist_confirmed_viewing_history(
                    _confirmed("1291546", source_row_number=9),
                    _detail("1291546", "Failed Movie"),
                )
                first_adapter = _FakeDetailPageAdapter(
                    {
                        "1292052": _detail("1292052", "Completed Movie"),
                        "1291546": _detail("1291546", "Failed Movie"),
                    },
                    {"1292052": html, "1291546": None},
                )

                first = discover_history_recommendations(repository, first_adapter)
                remaining_after_first = repository.count_unprocessed_watched_movies_for_history_recommendations()

                second_adapter = _FakeDetailPageAdapter(
                    {
                        "1292052": _detail("1292052", "Completed Movie"),
                        "1291546": _detail("1291546", "Failed Movie"),
                    },
                    {"1292052": html, "1291546": html},
                )
                second = discover_history_recommendations(repository, second_adapter)
                remaining_after_second = repository.count_unprocessed_watched_movies_for_history_recommendations()

        self.assertEqual(2, first.attempted_count)
        self.assertEqual(1, first.failed_count)
        self.assertEqual(1, remaining_after_first)
        self.assertEqual(["1291546"], second_adapter.fetches)
        self.assertEqual(1, second.attempted_count)
        self.assertEqual(0, second.failed_count)
        self.assertEqual(0, remaining_after_second)

    def test_parse_recommended_subject_ids_uses_recommendation_section_only(self) -> None:
        html = f"""
        <a href="https://movie.douban.com/subject/1111111/">outside</a>
        {_recommendations_html("电影", ("1292720", "1292052", "1292720", "1292064"))}
        <a href="https://movie.douban.com/subject/2222222/">outside</a>
        """

        self.assertEqual(["1292720", "1292064"], parse_recommended_subject_ids(html, "1292052"))

    def test_parse_recommended_subject_ids_accepts_series_recommendation_section(self) -> None:
        html = _recommendations_html("剧集", ("35465232", "35600000"))

        self.assertEqual(["35465232", "35600000"], parse_recommended_subject_ids(html, "1292052"))


class _FakeTop250Client:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    def fetch_page(self, start: int) -> str:
        if start > 0:
            return ""
        return "\n".join(f'<a href="https://movie.douban.com/subject/{subject_id}/">Movie</a>' for subject_id in self.ids)


class _FakeDetailPageAdapter:
    def __init__(self, details: dict[str, DoubanMovieDetail], pages: dict[str, str | None]) -> None:
        self.details = details
        self.pages = pages
        self.fetches: list[str] = []
        self._last_page_source: str | None = None

    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        self.fetches.append(subject_id)
        self._last_page_source = self.pages.get(subject_id)
        return self.details[subject_id]

    @property
    def last_page_source(self) -> str | None:
        return self._last_page_source


def _recommendations_html(kind: str, subject_ids: tuple[str, ...]) -> str:
    links = "\n".join(
        f'<dd><a href="https://movie.douban.com/subject/{subject_id}/?from=subject-page">{subject_id}</a></dd>'
        for subject_id in subject_ids
    )
    return f"""
    <div id="recommendations">
      <h2><i>喜欢这部{kind}的人也喜欢 · · · · · ·</i></h2>
      <div class="recommendations-bd">
        <dl>
          {links}
        </dl>
      </div>
    </div>
    """


def _confirmed(subject_id: str, source_row_number: int = 8):
    from datetime import date

    from backend.app.models.domain import ConfirmedViewingHistoryInput

    return ConfirmedViewingHistoryInput(
        source_raw_id=f"raw-{subject_id}",
        source_file="MOVIES.xlsx#2026",
        source_row_number=source_row_number,
        douban_subject_id=subject_id,
        watched_date=date(2026, 3, 19),
        user_rating=4.2,
        source_row_hash=f"hash-{subject_id}",
        quality="1080p",
        comment="watched",
    )


def _detail(subject_id: str, title: str) -> DoubanMovieDetail:
    return DoubanMovieDetail(
        subject_id=subject_id,
        title=title,
        year=1994,
        directors=("Frank Darabont",),
        actors=("Tim Robbins",),
        genres=("Drama",),
        countries=("United States",),
        douban_rating=9.7,
        douban_vote_count=3000000,
        url=f"https://movie.douban.com/subject/{subject_id}/",
    )


if __name__ == "__main__":
    unittest.main()
