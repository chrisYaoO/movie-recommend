import unittest

from backend.app.models.domain import DoubanSearchResult
from backend.app.services.matching_service import FakeDoubanSearchAdapter
from backend.app.services.movie_search_service import MovieSearchCandidate, MovieSearchService, extract_douban_subject_id


class MovieSearchServiceTest(unittest.TestCase):
    def test_search_returns_at_most_five_candidates(self) -> None:
        adapter = FakeDoubanSearchAdapter(
            {
                "Still Walking": [
                    DoubanSearchResult(subject_id=str(index), title=f"Movie {index}", year=2000 + index)
                    for index in range(6)
                ]
            }
        )
        service = MovieSearchService(adapter)

        results = service.search("  Still   Walking ")

        self.assertEqual(5, len(results))
        self.assertEqual("Still Walking", adapter.searches[0].title)
        self.assertEqual("0", results[0].subject_id)
        self.assertEqual(2000, results[0].year)

    def test_search_rejects_blank_query(self) -> None:
        service = MovieSearchService(FakeDoubanSearchAdapter())

        with self.assertRaisesRegex(ValueError, "query is required"):
            service.search("   ")

    def test_plain_douban_subject_id_returns_existing_local_movie(self) -> None:
        adapter = FakeDoubanSearchAdapter()
        lookup = _FakeLocalLookup(
            {
                "2222996": MovieSearchCandidate(
                    subject_id="2222996",
                    title="Still Walking",
                    year=2008,
                    director="Hirokazu Kore-eda",
                    url="https://movie.douban.com/subject/2222996/",
                )
            }
        )
        service = MovieSearchService(adapter, local_lookup=lookup)

        results = service.search("2222996")

        self.assertEqual(1, len(results))
        self.assertEqual("Still Walking", results[0].title)
        self.assertEqual(["2222996"], lookup.subject_ids)
        self.assertEqual([], adapter.searches)

    def test_douban_subject_url_returns_existing_local_movie(self) -> None:
        lookup = _FakeLocalLookup(
            {
                "2222996": MovieSearchCandidate(
                    subject_id="2222996",
                    title="Still Walking",
                    year=2008,
                    director="Hirokazu Kore-eda",
                    url="https://movie.douban.com/subject/2222996/",
                )
            }
        )
        service = MovieSearchService(FakeDoubanSearchAdapter(), local_lookup=lookup)

        results = service.search("https://movie.douban.com/subject/2222996/?from=showing")

        self.assertEqual("2222996", results[0].subject_id)
        self.assertEqual("Still Walking", results[0].title)

    def test_missing_local_metadata_subject_id_returns_safe_selectable_candidate(self) -> None:
        service = MovieSearchService(FakeDoubanSearchAdapter(), local_lookup=_FakeLocalLookup({}))

        results = service.search("2222996")

        self.assertEqual(
            [
                MovieSearchCandidate(
                    subject_id="2222996",
                    title="Douban subject 2222996",
                    year=None,
                    director=None,
                    url="https://movie.douban.com/subject/2222996/",
                )
            ],
            results,
        )

    def test_extract_douban_subject_id_accepts_plain_ids_and_subject_urls(self) -> None:
        self.assertEqual("2222996", extract_douban_subject_id("2222996"))
        self.assertEqual("2222996", extract_douban_subject_id("https://movie.douban.com/subject/2222996/"))
        self.assertIsNone(extract_douban_subject_id("Still Walking"))

    def test_to_response_serializes_candidates(self) -> None:
        service = MovieSearchService(FakeDoubanSearchAdapter())
        candidates = service.search("Missing")

        response = service.to_response("Missing", candidates)

        self.assertEqual({"query": "Missing", "items": []}, response)


class _FakeLocalLookup:
    def __init__(self, candidates: dict[str, MovieSearchCandidate]) -> None:
        self.candidates = candidates
        self.subject_ids = []

    def find_by_subject_id(self, subject_id: str) -> MovieSearchCandidate | None:
        self.subject_ids.append(subject_id)
        return self.candidates.get(subject_id)


if __name__ == "__main__":
    unittest.main()


