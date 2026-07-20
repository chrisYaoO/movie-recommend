from backend.app.services.import_service import RAW_HASH_COLUMNS, stable_row_hash
from jobs.audit_viewing_history_sheet_sync import _checksum_variants, _mapped_values, classify


def _local(id: str, row: int, checksum: str) -> dict:
    return {
        "id": id,
        "source_sheet_name": "2026",
        "source_row_number": row,
        "source_row_checksum": checksum,
        "douban_subject_id": id,
        "title": id,
    }


def _sheet(row: int, checksum: str, record_id: str | None = None) -> dict:
    return {
        "sheet_name": "2026",
        "row_number": row,
        "checksum": checksum,
        "record_id": record_id,
        "record_id_raw": record_id,
        "title": checksum,
    }


def test_classifies_all_relationships_without_writes() -> None:
    local = [
        _local("matched", 2, "a"),
        _local("relinked", 30, "b"),
        _local("local-only", 40, "c"),
        _local("conflict", 5, "old"),
        _local("ambiguous", 50, "duplicate"),
    ]
    sheets = [
        _sheet(2, "a"),
        _sheet(3, "b"),
        _sheet(5, "new"),
        _sheet(6, "duplicate"),
        _sheet(7, "duplicate"),
        _sheet(8, "sheet-only"),
    ]

    results = classify(local, sheets)
    statuses = {result["local"]["id"]: result["status"] for result in results if result["local"]}

    assert statuses == {
        "matched": "matched",
        "relinked": "relinked",
        "local-only": "local_only",
        "conflict": "content_conflict",
        "ambiguous": "ambiguous",
    }
    assert sum(result["status"] == "sheet_only" for result in results) == 1


def test_checksum_variants_treat_sheet_date_format_as_equivalent() -> None:
    values = {column: None for column in RAW_HASH_COLUMNS}
    values.update({"Date": "5/28", "Name": "Movie", "Rating": "4"})
    projected = {**values, "Date": "2026-05-28", "Rating": "4.0"}

    assert stable_row_hash(projected) in _checksum_variants(values, "2026")


def test_blank_legacy_headers_keep_positional_subject_and_image_ids() -> None:
    header = ["Date", "Name", "Director", "Year", "Ratings", "Quality", "Comments", "", "", "RecordId"]
    row = ["12/31", "Movie", "Director", "2013", "4.0 ", "蓝光", "comment", "10437779", "1903379979", "id"]

    values = _mapped_values(header, row)

    assert values["DoubanSubjectId"] == "10437779"
    assert values["DoubanImageId"] == "1903379979"


def test_same_movie_watched_twice_is_not_collapsed() -> None:
    first = _local("history-1", 2, "first")
    second = _local("history-2", 3, "second")
    first["douban_subject_id"] = second["douban_subject_id"] = "1291561"

    results = classify([first, second], [_sheet(2, "first"), _sheet(3, "second")])

    assert [result["status"] for result in results] == ["matched", "matched"]
    assert {result["local"]["id"] for result in results} == {"history-1", "history-2"}


def test_duplicate_record_id_is_reported_without_mutating_multiple_rows() -> None:
    record_id = "7b8557b5-922b-452d-bd75-e33a71472e87"

    results = classify(
        [_local(record_id, 2, "same")],
        [_sheet(2, "same", record_id), _sheet(3, "same", record_id)],
    )

    duplicate = next(result for result in results if result["status"] == "duplicate_record_id")
    assert duplicate["local"]["id"] == record_id
    assert len(duplicate["sheet_rows"]) == 2
