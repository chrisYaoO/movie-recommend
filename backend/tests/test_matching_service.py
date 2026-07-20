import unittest
from dataclasses import replace
from datetime import date
from tempfile import TemporaryDirectory

from backend.app.models.domain import DoubanMatchStatus, DoubanSearchResult, ViewingHistoryCandidate
from backend.app.services.matching_service import (
    CachedDoubanSearchAdapter,
    DoubanHttpSearchAdapter,
    FakeDoubanSearchAdapter,
    FileDoubanSearchCache,
    InMemoryDoubanSearchCache,
    METADATA_STRATEGY,
    SUBJECT_ID_STRATEGY,
    build_confirmed_viewing_history_inputs,
    build_auto_matched_viewing_history_inputs,
    build_douban_match_inputs,
    parse_douban_search_results,
    run_local_match_rules,
    run_search_match_job,
    search_cache_key,
    score_search_result,
)


class MatchingServiceTest(unittest.TestCase):
    def test_builds_subject_id_and_metadata_match_inputs(self) -> None:
        preview = build_douban_match_inputs(
            [
                ViewingHistoryCandidate(
                    source_raw_id="raw-1",
                    source_sheet_name="MOVIES.xlsx#2026",
                    source_row_number=2,
                    title="A Pale View of Hills",
                    user_rating=4.2,
                    release_year=2025,
                    director="Kei Ishikawa",
                    douban_subject_id="36913048",
                ),
                ViewingHistoryCandidate(
                    source_raw_id="raw-2",
                    source_sheet_name="MOVIES.xlsx#2025",
                    source_row_number=3,
                    title="Still Walking",
                    user_rating=4.5,
                    release_year=2008,
                    director="Hirokazu Kore-eda",
                ),
            ]
        )

        self.assertEqual(2, preview.total_count)
        self.assertEqual(1, preview.subject_id_count)
        self.assertEqual(1, preview.metadata_count)

        by_id = preview.inputs[0]
        self.assertEqual(SUBJECT_ID_STRATEGY, by_id.strategy)
        self.assertEqual("36913048", by_id.douban_subject_id)
        self.assertEqual("A Pale View of Hills", by_id.title)

        by_metadata = preview.inputs[1]
        self.assertEqual(METADATA_STRATEGY, by_metadata.strategy)
        self.assertIsNone(by_metadata.douban_subject_id)
        self.assertEqual(2008, by_metadata.release_year)
        self.assertEqual("Hirokazu Kore-eda", by_metadata.director)

    def test_local_match_rules_auto_match_subject_ids_and_queue_metadata_for_review(self) -> None:
        queue = build_douban_match_inputs(
            [
                ViewingHistoryCandidate(
                    source_raw_id="raw-1",
                    source_sheet_name="MOVIES.xlsx#2026",
                    source_row_number=2,
                    title="A Pale View of Hills",
                    user_rating=4.2,
                    release_year=2025,
                    director="Kei Ishikawa",
                    douban_subject_id="36913048",
                ),
                ViewingHistoryCandidate(
                    source_raw_id="raw-2",
                    source_sheet_name="MOVIES.xlsx#2025",
                    source_row_number=3,
                    title="Still Walking",
                    user_rating=4.5,
                    release_year=2008,
                    director="Hirokazu Kore-eda",
                ),
            ]
        )

        result = run_local_match_rules(queue.inputs)

        self.assertEqual(2, result.total_count)
        self.assertEqual(1, result.auto_matched_count)
        self.assertEqual(1, result.needs_review_count)

        auto_match = result.candidates[0]
        self.assertEqual(DoubanMatchStatus.AUTO_MATCHED, auto_match.status)
        self.assertEqual(1.0, auto_match.match_score)
        self.assertEqual(("excel_douban_subject_id",), auto_match.match_reasons)
        self.assertEqual("36913048", auto_match.candidate_subject_id)

        review = result.candidates[1]
        self.assertEqual(DoubanMatchStatus.NEEDS_REVIEW, review.status)
        self.assertEqual(0.0, review.match_score)
        self.assertEqual(("metadata_match_required",), review.match_reasons)
        self.assertEqual("Still Walking", review.candidate_title)

    def test_build_confirmed_viewing_history_inputs_uses_only_subject_id_and_user_history_fields(self) -> None:
        candidates = [
            ViewingHistoryCandidate(
                source_raw_id="raw-1",
                source_sheet_name="MOVIES.xlsx#2025",
                source_row_number=3,
                title="Still Walking",
                user_rating=4.5,
                watched_date=date(2025, 5, 1),
                director="Hirokazu Kore-eda",
                release_year=2008,
                quality="1080p",
                comment="great",
            ),
            ViewingHistoryCandidate(
                source_raw_id="raw-2",
                source_sheet_name="MOVIES.xlsx#2025",
                source_row_number=4,
                title="Unknown Movie",
                user_rating=4.0,
                watched_date=date(2025, 5, 2),
                comment="skip",
            ),
        ]
        confirmed_match = DoubanSearchResult(subject_id="2222996", title="Different Detail Title", year=2008)
        queue = build_douban_match_inputs(candidates)
        run_result = run_search_match_job(
            [queue.inputs[0], queue.inputs[1]],
            FakeDoubanSearchAdapter({"Still Walking": [confirmed_match]}),
        )
        confirmed = replace(run_result.candidates[0], status=DoubanMatchStatus.CONFIRMED)
        inputs = build_confirmed_viewing_history_inputs(candidates, [confirmed, *run_result.candidates[1:]])

        self.assertEqual(1, len(inputs))
        self.assertEqual("raw-1", inputs[0].source_raw_id)
        self.assertEqual("MOVIES.xlsx#2025", inputs[0].source_sheet_name)
        self.assertEqual(3, inputs[0].source_row_number)
        self.assertEqual("2222996", inputs[0].douban_subject_id)
        self.assertEqual(date(2025, 5, 1), inputs[0].watched_date)
        self.assertEqual(4.5, inputs[0].user_rating)
        self.assertIsNone(inputs[0].source_row_checksum)
        self.assertEqual("1080p", inputs[0].quality)
        self.assertEqual("great", inputs[0].comment)
        self.assertFalse(hasattr(inputs[0], "title"))
        self.assertFalse(hasattr(inputs[0], "release_year"))
        self.assertFalse(hasattr(inputs[0], "director"))

    def test_build_auto_matched_viewing_history_inputs_uses_auto_matches_only(self) -> None:
        candidates = [
            ViewingHistoryCandidate(
                source_raw_id="raw-1",
                source_sheet_name="MOVIES.xlsx#2025",
                source_row_number=3,
                title="Still Walking",
                user_rating=4.5,
                watched_date=date(2025, 5, 1),
                release_year=2008,
            ),
            ViewingHistoryCandidate(
                source_raw_id="raw-2",
                source_sheet_name="MOVIES.xlsx#2025",
                source_row_number=4,
                title="Bittersweet Life",
                user_rating=4.0,
                watched_date=date(2025, 5, 2),
                release_year=2025,
            ),
        ]
        queue = build_douban_match_inputs(candidates)
        run_result = run_search_match_job(
            queue.inputs,
            FakeDoubanSearchAdapter(
                {
                    "Still Walking": [DoubanSearchResult(subject_id="2222996", title="Still Walking", year=2008)],
                    "Bittersweet Life": [
                        DoubanSearchResult(subject_id="subject-review", title="Bitter Sweet Life", year=2025)
                    ],
                }
            ),
        )

        inputs = build_auto_matched_viewing_history_inputs(candidates, run_result.candidates)

        self.assertEqual(1, len(inputs))
        self.assertEqual("raw-1", inputs[0].source_raw_id)
        self.assertEqual("2222996", inputs[0].douban_subject_id)

    def test_search_match_job_uses_adapter_for_metadata_inputs_only(self) -> None:
        queue = build_douban_match_inputs(
            [
                ViewingHistoryCandidate(
                    source_raw_id="raw-1",
                    source_sheet_name="MOVIES.xlsx#2026",
                    source_row_number=2,
                    title="A Pale View of Hills",
                    user_rating=4.2,
                    douban_subject_id="36913048",
                ),
                ViewingHistoryCandidate(
                    source_raw_id="raw-2",
                    source_sheet_name="MOVIES.xlsx#2025",
                    source_row_number=3,
                    title="Still Walking",
                    user_rating=4.5,
                    release_year=2008,
                    director="Hirokazu Kore-eda",
                ),
                ViewingHistoryCandidate(
                    source_raw_id="raw-3",
                    source_sheet_name="MOVIES.xlsx#2025",
                    source_row_number=4,
                    title="Unknown Movie",
                    user_rating=4.0,
                ),
            ]
        )
        adapter = FakeDoubanSearchAdapter(
            {
                "Still Walking": [
                    DoubanSearchResult(
                        subject_id="2222996",
                        title="Still Walking",
                        year=2008,
                        director="Hirokazu Kore-eda",
                    )
                ]
            }
        )

        result = run_search_match_job(queue.inputs, adapter)

        self.assertEqual(2, len(adapter.searches))
        self.assertEqual(["Still Walking", "Unknown Movie"], [item.title for item in adapter.searches])
        self.assertEqual(3, result.total_count)
        self.assertEqual(DoubanMatchStatus.AUTO_MATCHED, result.candidates[0].status)

        auto_match = result.candidates[1]
        self.assertEqual(DoubanMatchStatus.AUTO_MATCHED, auto_match.status)
        self.assertEqual(0.95, auto_match.match_score)
        self.assertEqual(("title_year_exact",), auto_match.match_reasons)
        self.assertEqual("2222996", auto_match.candidate_subject_id)
        self.assertEqual("Still Walking", auto_match.candidate_title)

        no_match = result.candidates[2]
        self.assertEqual(DoubanMatchStatus.NO_MATCH, no_match.status)
        self.assertEqual(("douban_search_no_results",), no_match.match_reasons)

    def test_search_match_job_selects_first_result_matching_release_year(self) -> None:
        queue = build_douban_match_inputs(
            [
                ViewingHistoryCandidate(
                    source_raw_id="raw-1",
                    source_sheet_name="MOVIES.xlsx#2025",
                    source_row_number=3,
                    title="Still Walking",
                    user_rating=4.5,
                    release_year=2008,
                ),
            ]
        )
        adapter = FakeDoubanSearchAdapter(
            {
                "Still Walking": [
                    DoubanSearchResult(subject_id="wrong", title="Still Walking", year=2019),
                    DoubanSearchResult(subject_id="2222996", title="Still Walking", year=2008),
                ]
            }
        )

        result = run_search_match_job(queue.inputs, adapter)

        self.assertEqual(DoubanMatchStatus.AUTO_MATCHED, result.candidates[0].status)
        self.assertEqual("2222996", result.candidates[0].candidate_subject_id)

    def test_search_match_job_prefers_exact_title_before_first_year_match(self) -> None:
        queue = build_douban_match_inputs(
            [
                ViewingHistoryCandidate(
                    source_raw_id="raw-1",
                    source_sheet_name="MOVIES.xlsx#2023",
                    source_row_number=75,
                    title="娑堝け鐨勫ス",
                    user_rating=4.0,
                    release_year=2023,
                ),
            ]
        )
        adapter = FakeDoubanSearchAdapter(
            {
                "娑堝け鐨勫ス": [
                    DoubanSearchResult(subject_id="wrong", title="wrong sequel", year=2023),
                    DoubanSearchResult(subject_id="right", title="娑堝け鐨勫ス", year=2023),
                ]
            }
        )

        result = run_search_match_job(queue.inputs, adapter)

        self.assertEqual(DoubanMatchStatus.AUTO_MATCHED, result.candidates[0].status)
        self.assertEqual("right", result.candidates[0].candidate_subject_id)
        self.assertEqual(("title_year_exact",), result.candidates[0].match_reasons)

    def test_search_match_job_keeps_title_difference_for_review_even_when_year_matches(self) -> None:
        queue = build_douban_match_inputs(
            [
                ViewingHistoryCandidate(
                    source_raw_id="raw-1",
                    source_sheet_name="MOVIES.xlsx#2025",
                    source_row_number=3,
                    title="Bitter Sweet Life",
                    user_rating=4.5,
                    release_year=2025,
                ),
            ]
        )
        adapter = FakeDoubanSearchAdapter(
            {
                "Bitter Sweet Life": [
                    DoubanSearchResult(subject_id="subject", title="Bittersweet Life", year=2025),
                ]
            }
        )

        result = run_search_match_job(queue.inputs, adapter)

        self.assertEqual(DoubanMatchStatus.NEEDS_REVIEW, result.candidates[0].status)
        self.assertEqual(0.75, result.candidates[0].match_score)
        self.assertEqual(("year_match_title_differs",), result.candidates[0].match_reasons)

    def test_search_match_job_marks_needs_review_when_no_result_year_matches(self) -> None:
        queue = build_douban_match_inputs(
            [
                ViewingHistoryCandidate(
                    source_raw_id="raw-1",
                    source_sheet_name="MOVIES.xlsx#2025",
                    source_row_number=3,
                    title="Still Walking",
                    user_rating=4.5,
                    release_year=2008,
                ),
            ]
        )
        adapter = FakeDoubanSearchAdapter(
            {
                "Still Walking": [
                    DoubanSearchResult(subject_id="wrong", title="Walking Still", year=2019),
                ]
            }
        )

        result = run_search_match_job(queue.inputs, adapter)

        self.assertEqual(DoubanMatchStatus.NEEDS_REVIEW, result.candidates[0].status)
        self.assertEqual(("douban_search_no_year_match",), result.candidates[0].match_reasons)
        self.assertEqual("wrong", result.candidates[0].candidate_subject_id)

    def test_cached_search_adapter_reuses_results_by_normalized_metadata_key(self) -> None:
        first = ViewingHistoryCandidate(
            source_raw_id="raw-1",
            source_sheet_name="MOVIES.xlsx#2025",
            source_row_number=3,
            title="Still   Walking",
            user_rating=4.5,
            release_year=2008,
            director="Hirokazu Kore-eda",
        )
        second = ViewingHistoryCandidate(
            source_raw_id="raw-2",
            source_sheet_name="MOVIES.xlsx#2025",
            source_row_number=4,
            title="still walking",
            user_rating=4.4,
            release_year=2008,
            director="hirokazu   kore-eda",
        )
        queue = build_douban_match_inputs([first, second])
        inner = FakeDoubanSearchAdapter(
            {
                "Still   Walking": [
                    DoubanSearchResult(
                        subject_id="2222996",
                        title="Still Walking",
                        year=2008,
                        director="Hirokazu Kore-eda",
                    )
                ]
            }
        )
        adapter = CachedDoubanSearchAdapter(inner, InMemoryDoubanSearchCache())

        result = run_search_match_job(queue.inputs, adapter)

        self.assertEqual(1, len(inner.searches))
        self.assertEqual(1, adapter.miss_count)
        self.assertEqual(1, adapter.hit_count)
        self.assertEqual(["2222996", "2222996"], [item.candidate_subject_id for item in result.candidates])
        self.assertEqual(search_cache_key(queue.inputs[0]), search_cache_key(queue.inputs[1]))

    def test_cached_search_adapter_caches_empty_results(self) -> None:
        candidate = ViewingHistoryCandidate(
            source_raw_id="raw-1",
            source_sheet_name="MOVIES.xlsx#2025",
            source_row_number=3,
            title="Unknown Movie",
            user_rating=4.0,
        )
        queue = build_douban_match_inputs([candidate, candidate])
        inner = FakeDoubanSearchAdapter()
        adapter = CachedDoubanSearchAdapter(inner, InMemoryDoubanSearchCache())

        result = run_search_match_job(queue.inputs, adapter)

        self.assertEqual(1, len(inner.searches))
        self.assertEqual(1, adapter.miss_count)
        self.assertEqual(1, adapter.hit_count)
        self.assertEqual(
            [DoubanMatchStatus.NO_MATCH, DoubanMatchStatus.NO_MATCH],
            [item.status for item in result.candidates],
        )

    def test_cached_search_adapter_can_ignore_empty_cached_results(self) -> None:
        candidate = ViewingHistoryCandidate(
            source_raw_id="raw-1",
            source_sheet_name="MOVIES.xlsx#2025",
            source_row_number=3,
            title="肖申克",
            user_rating=4.0,
        )
        queue = build_douban_match_inputs([candidate])
        cache = InMemoryDoubanSearchCache()
        cache.set(queue.inputs[0], [])
        inner = FakeDoubanSearchAdapter(
            {
                "肖申克": [
                    DoubanSearchResult(
                        subject_id="1292052",
                        title="肖申克的救赎",
                        year=1994,
                    )
                ]
            }
        )
        adapter = CachedDoubanSearchAdapter(inner, cache, cache_empty_results=False)

        result = run_search_match_job(queue.inputs, adapter)

        self.assertEqual(1, len(inner.searches))
        self.assertEqual(1, adapter.miss_count)
        self.assertEqual(0, adapter.hit_count)
        self.assertEqual("1292052", result.candidates[0].candidate_subject_id)

    def test_score_search_result_uses_title_and_year(self) -> None:
        queue = build_douban_match_inputs(
            [
                ViewingHistoryCandidate(
                    source_raw_id="raw-1",
                    source_sheet_name="MOVIES.xlsx#2025",
                    source_row_number=3,
                    title="Still Walking",
                    user_rating=4.5,
                    release_year=2008,
                ),
            ]
        )
        match_input = queue.inputs[0]

        exact = score_search_result(
            match_input,
            DoubanSearchResult(subject_id="2222996", title="still   walking", year=2008),
        )
        different_title = score_search_result(
            match_input,
            DoubanSearchResult(subject_id="2222996", title="Walking Still", year=2008),
        )
        wrong_year = score_search_result(
            match_input,
            DoubanSearchResult(subject_id="2222996", title="Still Walking", year=2019),
        )
        wrong_title_and_year = score_search_result(
            match_input,
            DoubanSearchResult(subject_id="2222996", title="Walking Still", year=2019),
        )

        self.assertEqual(DoubanMatchStatus.AUTO_MATCHED, exact.status)
        self.assertEqual(0.95, exact.score)
        self.assertEqual(DoubanMatchStatus.NEEDS_REVIEW, different_title.status)
        self.assertEqual(0.75, different_title.score)
        self.assertEqual(DoubanMatchStatus.AUTO_MATCHED, wrong_year.status)
        self.assertEqual(0.9, wrong_year.score)
        self.assertEqual("title_exact_year_differs", wrong_year.reason)
        self.assertEqual(DoubanMatchStatus.NEEDS_REVIEW, wrong_title_and_year.status)
        self.assertEqual(0.5, wrong_title_and_year.score)

    def test_parse_douban_search_results_extracts_subject_title_year_and_director(self) -> None:
        html = """
        <div class="result">
          <h3>
            <a href="https://movie.douban.com/subject/2222996/">Still Walking</a>
          </h3>
          <span class="subject-cast">瀵兼紨: Hirokazu Kore-eda / 2008 / Japan</span>
        </div>
        <div class="result">
          <h3>
            <a href="https://movie.douban.com/subject/1291561/">Yi Yi</a>
          </h3>
          <span class="subject-cast">瀵兼紨: Edward Yang / 2000 / Taiwan</span>
        </div>
        """

        results = parse_douban_search_results(html)

        self.assertEqual(2, len(results))
        self.assertEqual("2222996", results[0].subject_id)
        self.assertEqual("Still Walking", results[0].title)
        self.assertEqual(2008, results[0].year)
        self.assertEqual("Hirokazu Kore-eda", results[0].director)
        self.assertEqual("https://movie.douban.com/subject/2222996/", results[0].url)

    def test_parse_douban_search_results_supports_link2_wrapped_subject_urls(self) -> None:
        html = """
        <div class="result">
          <a class="nbg" href="https://www.douban.com/link2/?url=https%3A%2F%2Fmovie.douban.com%2Fsubject%2F2222996%2F"
             onclick="moreurl(this,{sid: 2222996})"><img src="poster.jpg"></a>
          <div class="title">
            <a href="https://www.douban.com/link2/?url=https%3A%2F%2Fmovie.douban.com%2Fsubject%2F2222996%2F"
               onclick="moreurl(this,{sid: 2222996})">Still Walking</a>
          </div>
          <span class="subject-cast">瀵兼紨: Hirokazu Kore-eda / 2008 / Japan</span>
        </div>
        """

        results = parse_douban_search_results(html)

        self.assertEqual(1, len(results))
        self.assertEqual("2222996", results[0].subject_id)
        self.assertEqual("Still Walking", results[0].title)

    def test_parse_douban_search_results_supports_current_movie_search_markup(self) -> None:
        html = """
        <div class="result">
          <div class="pic">
            <a class="nbg" href="https://www.douban.com/link2/?url=https%3A%2F%2Fmovie.douban.com%2Fsubject%2F1292052%2F"
               onclick="moreurl(this,{sid: 1292052})" title="The Shawshank Redemption"><img src="poster.jpg"></a>
          </div>
          <div class="content">
            <div class="title">
              <h3>
                <span>[电影]</span>
                &nbsp;<a href="https://www.douban.com/link2/?url=https%3A%2F%2Fmovie.douban.com%2Fsubject%2F1292052%2F"
                   onclick="moreurl(this,{sid: 1292052})">肖申克的救赎 </a>
              </h3>
              <div class="rating-info">
                <span class="subject-cast">原名:The Shawshank Redemption / 弗兰克·德拉邦特 / 蒂姆·罗宾斯 / 1994</span>
              </div>
            </div>
          </div>
        </div>
        """

        results = parse_douban_search_results(html)

        self.assertEqual(1, len(results))
        self.assertEqual("1292052", results[0].subject_id)
        self.assertEqual("肖申克的救赎 The Shawshank Redemption", results[0].title)
        self.assertEqual(1994, results[0].year)
        self.assertEqual("弗兰克·德拉邦特", results[0].director)

    def test_parse_douban_search_results_applies_person_display_rule_when_original_name_exists(self) -> None:
        html = """
        <div class="result">
          <div class="title">
            <a href="https://movie.douban.com/subject/1/">Foreign Director Movie</a>
          </div>
          <span class="subject-cast">雷德利·斯科特 Ridley Scott / 1991</span>
        </div>
        <div class="result">
          <div class="title">
            <a href="https://movie.douban.com/subject/2/">Japanese Director Movie</a>
          </div>
          <span class="subject-cast">是枝裕和 Hirokazu Kore-eda / 2008</span>
        </div>
        """

        results = parse_douban_search_results(html)

        self.assertEqual("Ridley Scott", results[0].director)
        self.assertEqual("是枝裕和", results[1].director)

    def test_file_search_cache_round_trips_results(self) -> None:
        candidate = ViewingHistoryCandidate(
            source_raw_id="raw-1",
            source_sheet_name="MOVIES.xlsx#2025",
            source_row_number=3,
            title="Still Walking",
            user_rating=4.5,
            release_year=2008,
            director="Hirokazu Kore-eda",
        )
        match_input = build_douban_match_inputs([candidate]).inputs[0]
        result = DoubanSearchResult(
            subject_id="2222996",
            title="Still Walking",
            year=2008,
            director="Hirokazu Kore-eda",
            url="https://movie.douban.com/subject/2222996/",
        )

        with TemporaryDirectory() as directory:
            cache = FileDoubanSearchCache(directory)
            cache.set(match_input, [result])
            loaded = cache.get(match_input)

        self.assertEqual([result], loaded)

    def test_http_adapter_builds_movie_search_url_from_metadata(self) -> None:
        candidate = ViewingHistoryCandidate(
            source_raw_id="raw-1",
            source_sheet_name="MOVIES.xlsx#2025",
            source_row_number=3,
            title="Still Walking",
            user_rating=4.5,
            release_year=2008,
            director="Hirokazu Kore-eda",
        )
        match_input = build_douban_match_inputs([candidate]).inputs[0]
        adapter = DoubanHttpSearchAdapter(base_url="https://www.douban.com/search")

        url = adapter._build_search_url(match_input)

        self.assertIn("cat=1002", url)
        self.assertIn("Still+Walking", url)
        self.assertNotIn("2008", url)
        self.assertNotIn("Hirokazu", url)


if __name__ == "__main__":
    unittest.main()



