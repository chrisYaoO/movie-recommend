from fastapi import APIRouter, HTTPException, Query

from backend.app.services.recommendation_service import FeedbackRequest, RecordWatchedRequest, service

router = APIRouter()


@router.get("/recommendations")
def get_recommendations(strategy: str = Query(default="hybrid")):
    try:
        session = service.recommend(strategy=strategy)
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
