from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import logging
import re
from uuid import UUID, uuid4

from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail
from backend.app.services.display_text import display_person_names
from backend.app.services.import_service import RAW_HASH_COLUMNS, stable_row_hash
from backend.app.services.metadata_service import DoubanDetailAdapter
from backend.app.services.viewing_history_sync_service import ViewingHistorySyncService
from jobs.candidate_pool import DOUBAN_RECOMMENDATION_SOURCE, parse_recommended_subject_ids


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordViewingHistoryRequest:
    douban_subject_id: str
    watched_date: date
    rating: float
    sheet: str
    session_id: str | None = None
    recommendation_item_id: str | None = None
    wishlist_id: str | None = None
    title: str | None = None
    director: str | None = None
    year: int | None = None
    quality: str | None = None
    comment: str | None = None
    history_id: str | None = None


@dataclass(frozen=True)
class RecordViewingHistoryResult:
    movie_id: str | None
    viewing_history_id: str
    douban_subject_id: str
    title: str | None
    source_sheet_name: str
    source_row_number: int
    source_row_checksum: str
    sheet_updated_range: str | None
    fetched_movie_detail: bool
    recommendation_inserted_count: int
    sync_state: str = "synced"


class ViewingHistoryRecordService:
    def __init__(
        self,
        repository: ViewingHistoryRepository,
        detail_adapter: DoubanDetailAdapter,
        syncer: ViewingHistorySyncService,
    ) -> None:
        self.repository = repository
        self.detail_adapter = detail_adapter
        self.syncer = syncer

    def record(self, request: RecordViewingHistoryRequest) -> RecordViewingHistoryResult:
        if not request.douban_subject_id.strip():
            raise ValueError("douban_subject_id is required")
        if not request.sheet.strip():
            raise ValueError("sheet is required")
        if not 0 <= request.rating <= 5:
            raise ValueError("rating must be between 0 and 5")
        history_id = str(UUID(request.history_id)) if request.history_id else str(uuid4())

        subject_id = request.douban_subject_id.strip()
        existing_history = self.repository.find_viewing_history(history_id, include_deleted=True)
        if existing_history and existing_history.douban_subject_id != subject_id:
            raise ValueError("history_id already belongs to another movie")
        existing_movie = self.repository.find_movie_by_subject_id(subject_id)
        detail = None
        if existing_movie is None:
            detail = self.detail_adapter.fetch(subject_id)
            title = detail.title
            director = ", ".join(display_person_names(detail.directors))
            year = detail.year
            image_id = _douban_image_id_from_url(detail.poster_url)
        else:
            title = existing_movie.title
            director = ", ".join(display_person_names(existing_movie.directors)) or request.director
            year = existing_movie.year or request.year
            image_id = _douban_image_id_from_url(existing_movie.poster_url)

        row_values = _sheet_row_values(
            title=title,
            director=director,
            year=year,
            subject_id=subject_id,
            image_id=image_id,
            request=request,
        )
        row_hash = _source_row_checksum(row_values)
        confirmed = ConfirmedViewingHistoryInput(
            source_raw_id=f"local:{history_id}",
            source_sheet_name=str(request.watched_date.year),
            source_row_number=existing_history.source_row_number if existing_history else 0,
            douban_subject_id=subject_id,
            watched_date=request.watched_date,
            user_rating=request.rating,
            source_row_checksum=row_hash,
            quality=request.quality,
            comment=request.comment,
            history_id=history_id,
        )
        recommendation_inserted_count = 0
        if detail is not None:
            movie = self.repository.upsert_movie_detail(detail)
            history = self.repository.save_viewing_history_and_enqueue(confirmed, movie.id)
            movie_id = movie.id
            history_id = history.id
            page_source = getattr(self.detail_adapter, "last_page_source", None)
            if page_source:
                try:
                    for recommended_id in parse_recommended_subject_ids(page_source, detail.subject_id):
                        if self.repository.upsert_candidate_subject(
                            recommended_id,
                            source_type=DOUBAN_RECOMMENDATION_SOURCE,
                            source_ref=f"recommended_from:{detail.subject_id}",
                            source_subject_id=detail.subject_id,
                            source_label=f"recommended from {detail.title}",
                        ):
                            recommendation_inserted_count += 1
                except Exception:
                    logger.exception("Recommendation discovery failed after viewing history was saved")
        else:
            history = self.repository.save_viewing_history_and_enqueue(confirmed, existing_movie.id)
            movie_id = existing_movie.id
            history_id = history.id
        self.syncer.sync_pending()
        current = self.repository.find_viewing_history(history_id, include_deleted=True)
        sync_state = "synced"
        if current and current.sync_operation:
            sync_state = "failed" if current.sync_attempts else "pending"
        return RecordViewingHistoryResult(
            movie_id=movie_id,
            viewing_history_id=history_id,
            douban_subject_id=subject_id,
            title=title,
            source_sheet_name=confirmed.source_sheet_name,
            source_row_number=current.source_row_number if current else confirmed.source_row_number,
            source_row_checksum=row_hash,
            sheet_updated_range=(
                f"{current.source_sheet_name}!A{current.source_row_number}:J{current.source_row_number}"
                if current and current.source_row_number >= 2 and sync_state == "synced"
                else None
            ),
            sync_state=sync_state,
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
    image_id: str | None,
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
        image_id or "",
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


def _douban_image_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/p(\d+)\.[A-Za-z0-9]+(?:[?#].*)?$", url)
    return match.group(1) if match else None


