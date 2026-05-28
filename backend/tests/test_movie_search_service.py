import unittest

from backend.app.models.domain import DoubanSearchResult
from backend.app.services.matching_service import FakeDoubanSearchAdapter
from backend.app.services.movie_search_service import MovieSearchService


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

    def test_to_response_serializes_candidates(self) -> None:
        service = MovieSearchService(FakeDoubanSearchAdapter())
        candidates = service.search("Missing")

        response = service.to_response("Missing", candidates)

        self.assertEqual({"query": "Missing", "items": []}, response)


if __name__ == "__main__":
    unittest.main()


