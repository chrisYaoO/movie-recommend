from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Protocol

from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail
from backend.app.services.google_sheets_service import GoogleSheetsAppendService
from backend.app.services.import_service import RAW_HASH_COLUMNS, stable_row_hash
from backend.app.services.metadata_service import DoubanDetailAdapter
from jobs.candidate_pool import DOUBAN_RECOMMENDATION_SOURCE, parse_recommended_subject_ids


@dataclass(frozen=True)
class RecordViewingHistoryRequest:
    douban_subject_id: str
    watched_date: date
    rating: float
    sheet: str
    title: str | None = None
    director: str | None = None
    year: int | None = None
    quality: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class RecordViewingHistoryResult:
    movie_id: str | None
    viewing_history_id: str
    douban_subject_id: str
    title: str | None
    source_sheet_name: str
    source_row_number: int
    source_row_checksum: str
    sheet_updated_range: str
    fetched_movie_detail: bool
    recommendation_inserted_count: int


class RecordViewingHistoryRepository(ViewingHistoryRepository, Protocol):
    pass


class ViewingHistoryRecordService:
    def __init__(
        self,
        repository: ViewingHistoryRepository,
        detail_adapter: DoubanDetailAdapter,
        sheets: GoogleSheetsAppendService,
    ) -> None:
        self.repository = repository
        self.detail_adapter = detail_adapter
        self.sheets = sheets

    def record(self, request: RecordViewingHistoryRequest) -> RecordViewingHistoryResult:
        if not request.douban_subject_id.strip():
            raise ValueError("douban_subject_id is required")
        if not request.sheet.strip():
            raise ValueError("sheet is required")

        subject_id = request.douban_subject_id.strip()
        existing_movie = self.repository.find_movie_by_subject_id(subject_id)
        detail = None
        if existing_movie is None:
            detail = self.detail_adapter.fetch(subject_id)
            title = detail.title
            director = ", ".join(detail.directors)
            year = detail.year
        else:
            title = existing_movie.title
            director = request.director
            year = request.year

        row_values = _sheet_row_values(
            title=title,
            director=director,
            year=year,
            subject_id=subject_id,
            request=request,
        )
        appended = self.sheets.append_viewing_history_row(request.sheet.strip(), row_values)
        row_hash = _source_row_checksum(row_values)
        confirmed = ConfirmedViewingHistoryInput(
            source_raw_id=f"google-sheets:{request.sheet}:{appended.row_number}",
            source_sheet_name=request.sheet.strip(),
            source_row_number=appended.row_number,
            douban_subject_id=subject_id,
            watched_date=request.watched_date,
            user_rating=request.rating,
            source_row_checksum=row_hash,
            quality=request.quality,
            comment=request.comment,
        )
        recommendation_inserted_count = 0
        if detail is not None:
            persisted = self.repository.persist_confirmed_viewing_history(confirmed, detail)
            movie_id = persisted.movie.id
            history_id = persisted.history.id
            page_source = getattr(self.detail_adapter, "last_page_source", None)
            if page_source:
                for recommended_id in parse_recommended_subject_ids(page_source, detail.subject_id):
                    if self.repository.upsert_candidate_subject(
                        recommended_id,
                        source_type=DOUBAN_RECOMMENDATION_SOURCE,
                        source_ref=f"recommended_from:{detail.subject_id}",
                        source_subject_id=detail.subject_id,
                        source_label=f"recommended from {detail.title}",
                    ):
                        recommendation_inserted_count += 1
        else:
            history = self.repository.upsert_viewing_history(confirmed, existing_movie.id)
            movie_id = existing_movie.id
            history_id = history.id
        return RecordViewingHistoryResult(
            movie_id=movie_id,
            viewing_history_id=history_id,
            douban_subject_id=subject_id,
            title=title,
            source_sheet_name=confirmed.source_sheet_name,
            source_row_number=confirmed.source_row_number,
            source_row_checksum=row_hash,
            sheet_updated_range=appended.updated_range,
            fetched_movie_detail=detail is not None,
            recommendation_inserted_count=recommendation_inserted_count,
        )

    def to_response(self, result: RecordViewingHistoryResult) -> dict:
        return asdict(result)


def _sheet_row_values(
    title: str | None,
    director: str | None,
    year: int | None,
    subject_id: str,
    request: RecordViewingHistoryRequest,
) -> list[str | float | int]:
    return [
        request.watched_date.isoformat(),
        title or request.title or "",
        director or request.director or "",
        year or request.year or "",
        request.rating,
        request.quality or "",
        request.comment or "",
        subject_id,
        "",
    ]


def _source_row_checksum(row_values: list[str | float | int]) -> str:
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


