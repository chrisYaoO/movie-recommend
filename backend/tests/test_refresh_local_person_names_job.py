import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.app.models.domain import DoubanMovieDetail
from jobs.refresh_local_person_names import (
    MovieRefreshCandidate,
    PersonNameIssue,
    find_refresh_candidates,
    refresh_local_person_names,
)


class RefreshLocalPersonNamesJobTest(unittest.TestCase):
    def test_finds_only_english_names_with_known_cjk_name_without_middle_dot(self) -> None:
        rows = [
            _row(
                "source",
                directors=["张艺谋 Yimou Zhang", "克里斯托弗·诺兰 Christopher Nolan"],
                actors=["陈虹妤 Shirley Chen"],
            ),
            _row("target", directors=["Yimou Zhang"], actors=["Shirley Chen"]),
            _row("western", directors=["Christopher Nolan"], countries=["美国"]),
        ]

        candidates = find_refresh_candidates(rows)

        self.assertEqual(["target"], [candidate.subject_id for candidate in candidates])
        self.assertEqual(
            [
                PersonNameIssue("director", "Yimou Zhang", ("张艺谋",)),
                PersonNameIssue("actor", "Shirley Chen", ("陈虹妤",)),
            ],
            list(candidates[0].issues),
        )

    def test_east_asian_movies_with_unmapped_english_names_are_refresh_candidates(self) -> None:
        rows = [
            _row(
                "japanese",
                directors=["Hayao Miyazaki"],
                actors=["Lynn Lynn"],
                countries=["日本"],
            ),
            _row(
                "american",
                directors=["Christopher Nolan"],
                actors=["Emma Stone"],
                countries=["美国"],
            ),
        ]

        candidates = find_refresh_candidates(rows)

        self.assertEqual(["japanese"], [candidate.subject_id for candidate in candidates])
        self.assertEqual(
            [
                PersonNameIssue("director", "Hayao Miyazaki", ()),
                PersonNameIssue("actor", "Lynn Lynn", ()),
            ],
            list(candidates[0].issues),
        )

    def test_checkpoint_skips_successes_and_retries_unresolved_metadata(self) -> None:
        candidates = [
            MovieRefreshCandidate(
                subject_id="one",
                title="One",
                issues=(PersonNameIssue("director", "Yimou Zhang", ("张艺谋",)),),
            ),
            MovieRefreshCandidate(
                subject_id="two",
                title="Two",
                issues=(PersonNameIssue("director", "Hirokazu Kore-eda", ("是枝裕和",)),),
            ),
        ]

        with TemporaryDirectory() as directory:
            checkpoint_path = Path(directory) / "progress.json"
            first_repository = _FakeRepository()
            first_adapter = _FakeAdapter(
                {
                    "one": _detail("one", "张艺谋 Yimou Zhang"),
                    "two": ValueError("invalid metadata"),
                }
            )

            first = refresh_local_person_names(
                first_repository,
                first_adapter,
                candidates,
                checkpoint_path,
            )

            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(2, first.attempted_count)
            self.assertEqual(1, first.updated_count)
            self.assertEqual(1, first.failed_count)
            self.assertEqual(["one"], list(checkpoint["completed"]))
            self.assertEqual(["two"], list(checkpoint["failures"]))
            self.assertEqual(["one"], [detail.subject_id for detail in first_repository.upserts])

            second_repository = _FakeRepository()
            second_adapter = _FakeAdapter(
                {
                    "one": _detail("one", "张艺谋 Yimou Zhang"),
                    "two": _detail("two", "是枝裕和 Hirokazu Kore-eda"),
                }
            )

            second = refresh_local_person_names(
                second_repository,
                second_adapter,
                candidates,
                checkpoint_path,
            )

            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(1, second.skipped_completed_count)
            self.assertEqual(1, second.attempted_count)
            self.assertEqual(1, second.updated_count)
            self.assertEqual(0, second.failed_count)
            self.assertEqual(["two"], second_adapter.fetches)
            self.assertEqual(["one", "two"], list(checkpoint["completed"]))
            self.assertEqual({}, checkpoint["failures"])

    def test_valid_json_ld_remains_authoritative_when_name_is_still_english(self) -> None:
        candidate = MovieRefreshCandidate(
            subject_id="one",
            title="One",
            issues=(PersonNameIssue("director", "Hayao Miyazaki", ()),),
        )

        with TemporaryDirectory() as directory:
            repository = _FakeRepository()
            summary = refresh_local_person_names(
                repository,
                _FakeAdapter({"one": _detail("one", "Hayao Miyazaki")}),
                [candidate],
                Path(directory) / "progress.json",
            )

        self.assertEqual(1, summary.updated_count)
        self.assertEqual(["one"], [detail.subject_id for detail in repository.upserts])


class _FakeRepository:
    def __init__(self) -> None:
        self.upserts = []

    def upsert_movie_detail(self, detail):
        self.upserts.append(detail)
        return detail


class _FakeAdapter:
    def __init__(self, details) -> None:
        self.details = details
        self.fetches = []

    def fetch(self, subject_id):
        self.fetches.append(subject_id)
        result = self.details[subject_id]
        if isinstance(result, Exception):
            raise result
        return result


def _row(subject_id, directors=(), actors=(), countries=()):
    return {
        "douban_subject_id": subject_id,
        "title": subject_id.title(),
        "countries": list(countries),
        "raw_douban_json": {
            "directors": list(directors),
            "actors": list(actors),
            "countries": list(countries),
        },
    }


def _detail(subject_id, director):
    return DoubanMovieDetail(
        subject_id=subject_id,
        title=subject_id.title(),
        directors=(director,),
        actors=(),
    )


if __name__ == "__main__":
    unittest.main()
