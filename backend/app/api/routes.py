import os
from threading import Lock

from fastapi import APIRouter, HTTPException, Query

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.services.google_sheets_service import GoogleSheetsValuesAppendService
from backend.app.services.movie_search_service import create_movie_search_service
from backend.app.services.metadata_service import DEFAULT_CHROME_BINARY_PATH, DoubanSeleniumDetailAdapter
from backend.app.services.recommendation_service import FeedbackRequest, RecordWatchedRequest, service
from backend.app.services.viewing_history_record_service import RecordViewingHistoryRequest, ViewingHistoryRecordService
from jobs.import_auto_matched_history import resolve_postgres_dsn
from jobs.sync_google_sheets_history import resolve_service_account_file, resolve_spreadsheet_id

router = APIRouter()
movie_search_service = create_movie_search_service()
viewing_history_record_service: ViewingHistoryRecordService | None = None
viewing_history_record_service_lock = Lock()
RECORD_CHROME_BINARY_ENV = "MOVIES_RECORD_CHROME_BINARY_PATH"
PREWARM_RECORD_SELENIUM_ENV = "MOVIES_PREWARM_RECORD_SELENIUM"


def create_record_detail_adapter():
    chrome_binary_path = os.getenv(RECORD_CHROME_BINARY_ENV, DEFAULT_CHROME_BINARY_PATH)
    return DoubanSeleniumDetailAdapter(chrome_binary_path=chrome_binary_path)


def get_viewing_history_record_service() -> ViewingHistoryRecordService:
    global viewing_history_record_service
    if viewing_history_record_service is not None:
        return viewing_history_record_service
    with viewing_history_record_service_lock:
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
                detail_adapter=create_record_detail_adapter(),
                sheets=GoogleSheetsValuesAppendService(
                    spreadsheet_id=spreadsheet_id,
                    service_account_file=service_account_file,
                ),
            )
    return viewing_history_record_service


def should_prewarm_record_selenium() -> bool:
    configured = os.getenv(PREWARM_RECORD_SELENIUM_ENV)
    if configured is not None:
        return configured.strip().lower() not in {"0", "false", "no", "off"}
    return os.getenv("MOVIES_DESKTOP") == "1"


def prewarm_viewing_history_record_service() -> None:
    get_viewing_history_record_service().detail_adapter.prewarm()


def close_viewing_history_record_service() -> None:
    global viewing_history_record_service
    with viewing_history_record_service_lock:
        record_service = viewing_history_record_service
        viewing_history_record_service = None
    if record_service is None:
        return
    record_service.detail_adapter.close()
    record_service.repository.close()


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
        processed_item = None
        if result.movie_id:
            service.mark_watched_movie(result.movie_id)
        if request.session_id and request.recommendation_item_id:
            processed_item = service.mark_watched_from_recommendation(
                request.session_id,
                request.recommendation_item_id,
                result.movie_id,
            )
        watched_wishlist_item = None
        if request.wishlist_id:
            watched_wishlist_item = service.mark_wishlist_item_watched_from_record(request.wishlist_id, result.movie_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"record viewing history failed: {exc}") from exc
    response = record_service.to_response(result)
    if request.session_id and request.recommendation_item_id:
        response["session_id"] = request.session_id
        response["recommendation_item_id"] = request.recommendation_item_id
        response["processing_status"] = processed_item.processing_status.value if processed_item else None
        response["processed_at"] = processed_item.processed_at.isoformat() if processed_item and processed_item.processed_at else None
    if request.wishlist_id:
        response["wishlist_id"] = request.wishlist_id
        response["wishlist_status"] = watched_wishlist_item.status.value if watched_wishlist_item else None
    return response


@router.get("/recommendations")
def get_recommendations(
    strategy: str = Query(default="hybrid"),
    seed: int | None = Query(default=None),
    exposure_cooldown_sessions: int = Query(default=5, ge=0),
):
    try:
        session = service.recommend(
            strategy=strategy,
            explore_seed=seed,
            exposure_cooldown_sessions=exposure_cooldown_sessions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.to_session_response(session)


@router.get("/recommendations/{session_id}")
def get_recommendation_session(session_id: str):
    try:
        session = service.get_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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


@router.delete("/recommendations/{session_id}/items/{item_id}/processing")
def undo_recommendation_item_processing(session_id: str, item_id: str):
    try:
        item = service.undo_recommendation_item_processing(session_id, item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.to_recommendation_item_response(item)


@router.get("/wishlist")
def get_wishlist(limit: int = Query(default=10, ge=1, le=50), offset: int = Query(default=0, ge=0)):
    return service.to_wishlist_response(limit=limit, offset=offset)


@router.post("/wishlist/{wishlist_id}/watched")
def record_watched(wishlist_id: str, request: RecordWatchedRequest):
    try:
        history = service.record_watched(wishlist_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.to_viewing_history_response(history)


@router.delete("/wishlist/{wishlist_id}")
def remove_from_wishlist(wishlist_id: str):
    try:
        item = service.remove_from_wishlist(wishlist_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service.to_wishlist_item_response(item)


@router.get("/not-interested")
def get_not_interested(limit: int = Query(default=10, ge=1, le=50), offset: int = Query(default=0, ge=0)):
    return service.to_not_interested_response(limit=limit, offset=offset)


@router.delete("/not-interested/{movie_id}")
def clear_not_interested(movie_id: str):
    try:
        item = service.clear_not_interested(movie_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service.to_not_interested_item_response(item)
