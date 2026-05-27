from fastapi import APIRouter, HTTPException, Query

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.services.google_sheets_service import GoogleSheetsValuesAppendService
from backend.app.services.movie_search_service import create_movie_search_service
from backend.app.services.metadata_service import DoubanHttpDetailAdapter
from backend.app.services.recommendation_service import FeedbackRequest, RecordWatchedRequest, service
from backend.app.services.viewing_history_record_service import RecordViewingHistoryRequest, ViewingHistoryRecordService
from jobs.import_auto_matched_history import resolve_postgres_dsn
from jobs.sync_google_sheets_history import resolve_service_account_file, resolve_spreadsheet_id

router = APIRouter()
movie_search_service = create_movie_search_service()
viewing_history_record_service: ViewingHistoryRecordService | None = None


def get_viewing_history_record_service() -> ViewingHistoryRecordService:
    global viewing_history_record_service
    if viewing_history_record_service is None:
        config_path = ".env"
        spreadsheet_id = resolve_spreadsheet_id(config_path)
        service_account_file = resolve_service_account_file(config_path)
        if not spreadsheet_id or not service_account_file:
            raise RuntimeError("Google Sheets spreadsheet id and service account file are required")
        repository = PostgresViewingHistoryRepository(resolve_postgres_dsn(None, config_path))
        repository.initialize_schema()
        viewing_history_record_service = ViewingHistoryRecordService(
            repository=repository,
            detail_adapter=DoubanHttpDetailAdapter(),
            sheets=GoogleSheetsValuesAppendService(
                spreadsheet_id=spreadsheet_id,
                service_account_file=service_account_file,
            ),
        )
    return viewing_history_record_service


@router.get("/movies/search")
def search_movies(q: str = Query(min_length=1)):
    try:
        candidates = movie_search_service.search(q)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"movie search failed: {exc}") from exc
    return movie_search_service.to_response(q, candidates)


@router.post("/viewing-history")
def record_viewing_history(request: RecordViewingHistoryRequest):
    try:
        record_service = get_viewing_history_record_service()
        result = record_service.record(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"record viewing history failed: {exc}") from exc
    return record_service.to_response(result)


@router.get("/recommendations")
def get_recommendations(strategy: str = Query(default="hybrid"), seed: int | None = Query(default=None)):
    try:
        session = service.recommend(strategy=strategy, explore_seed=seed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.to_session_response(session)


@router.post("/recommendations/{session_id}/items/{item_id}/feedback")
def submit_feedback(session_id: str, item_id: str, request: FeedbackRequest):
    try:
        feedback = service.submit_feedback(session_id, item_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.to_feedback_response(feedback)


@router.get("/wishlist")
def get_wishlist():
    return service.to_wishlist_response()


@router.post("/wishlist/{wishlist_id}/watched")
def record_watched(wishlist_id: str, request: RecordWatchedRequest):
    try:
        history = service.record_watched(wishlist_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.to_viewing_history_response(history)
