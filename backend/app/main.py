from contextlib import asynccontextmanager
import logging
from threading import Thread

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.config import load_local_env

load_local_env()

from backend.app.api.routes import (
    close_candidate_queue_service,
    close_viewing_history_record_service,
    prewarm_viewing_history_record_service,
    router,
    should_prewarm_record_selenium,
    stop_viewing_history_sync,
    sync_pending_viewing_history,
)

logger = logging.getLogger("uvicorn.error")


def _prewarm_record_selenium() -> None:
    try:
        prewarm_viewing_history_record_service()
        logger.info("Record Selenium driver prewarmed")
    except Exception:
        logger.exception("Record Selenium driver prewarm failed")


def _sync_pending_viewing_history() -> None:
    try:
        sync_pending_viewing_history()
    except Exception:
        logger.exception("Viewing-history Sheet sync failed during startup")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    sync_thread = Thread(target=_sync_pending_viewing_history, name="viewing-history-sheet-sync", daemon=True)
    sync_thread.start()
    if should_prewarm_record_selenium():
        Thread(target=_prewarm_record_selenium, name="record-selenium-prewarm", daemon=True).start()
    try:
        yield
    finally:
        stop_viewing_history_sync()
        sync_thread.join(timeout=25)
        close_candidate_queue_service()
        close_viewing_history_record_service()


app = FastAPI(title="Personal Movie Recommender", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "null",
    ],
    allow_origin_regex=r"^file://.*$",
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
