from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Protocol

from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail
from backend.app.services.google_sheets_service import GoogleSheetsAppendService
from backend.app.services.import_service import RAW_HASH_COLUMNS, stable_row_hash
from backend.app.services.metadata_service import DoubanDetailAdapter


@dataclass(frozen=True)
class RecordViewingHistoryRequest:
    douban_subject_id: str
    watched_date: date
    rating: float
    sheet: str
    quality: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class RecordViewingHistoryResult:
    movie_id: str
    viewing_history_id: str
    douban_subject_id: str
    title: str
    source_file: str
    source_row_number: int
    source_row_hash: str
    sheet_updated_range: str


class RecordViewingHistoryRepository(ViewingHistoryRepository, Protocol):
    pass


class ViewingHistoryRecordService:
    def __init__(
        self,
        repository: ViewingHistoryRepository,
        detail_adapter: DoubanDetailAdapter,
        sheets: GoogleSheetsAppendService,
        source_file_alias: str = "MOVIES.xlsx",
    ) -> None:
        self.repository = repository
        self.detail_adapter = detail_adapter
        self.sheets = sheets
        self.source_file_alias = source_file_alias

    def record(self, request: RecordViewingHistoryRequest) -> RecordViewingHistoryResult:
        if not request.douban_subject_id.strip():
            raise ValueError("douban_subject_id is required")
        if not request.sheet.strip():
            raise ValueError("sheet is required")

        detail = self.detail_adapter.fetch(request.douban_subject_id.strip())
        row_values = _sheet_row_values(detail, request)
        appended = self.sheets.append_viewing_history_row(request.sheet.strip(), row_values)
        row_hash = _source_row_hash(row_values)
        confirmed = ConfirmedViewingHistoryInput(
            source_raw_id=f"google-sheets:{request.sheet}:{appended.row_number}",
            source_file=f"{self.source_file_alias}#{request.sheet.strip()}",
            source_row_number=appended.row_number,
            douban_subject_id=detail.subject_id,
            watched_date=request.watched_date,
            user_rating=request.rating,
            source_row_hash=row_hash,
            quality=request.quality,
            comment=request.comment,
        )
        persisted = self.repository.persist_confirmed_viewing_history(confirmed, detail)
        return RecordViewingHistoryResult(
            movie_id=persisted.movie.id,
            viewing_history_id=persisted.history.id,
            douban_subject_id=detail.subject_id,
            title=detail.title,
            source_file=confirmed.source_file,
            source_row_number=confirmed.source_row_number,
            source_row_hash=row_hash,
            sheet_updated_range=appended.updated_range,
        )

    def to_response(self, result: RecordViewingHistoryResult) -> dict:
        return asdict(result)


def _sheet_row_values(detail: DoubanMovieDetail, request: RecordViewingHistoryRequest) -> list[str | float | int]:
    return [
        request.watched_date.isoformat(),
        detail.title,
        ", ".join(detail.directors),
        detail.year or "",
        request.rating,
        request.quality or "",
        request.comment or "",
        detail.subject_id,
        "",
    ]


def _source_row_hash(row_values: list[str | float | int]) -> str:
    values = {
        "Date": str(row_values[0]) if row_values[0] != "" else None,
        "Name": str(row_values[1]) if row_values[1] != "" else None,
        "Director": str(row_values[2]) if row_values[2] != "" else None,
        "Year": str(row_values[3]) if row_values[3] != "" else None,
        "Rating": str(row_values[4]) if row_values[4] != "" else None,
        "Quality": str(row_values[5]) if row_values[5] != "" else None,
        "Comment": str(row_values[6]) if row_values[6] != "" else None,
        "DoubanSubjectId": str(row_values[7]) if row_values[7] != "" else None,
        "DoubanImageId": str(row_values[8]) if row_values[8] != "" else None,
    }
    return stable_row_hash({column: values.get(column) for column in RAW_HASH_COLUMNS})
