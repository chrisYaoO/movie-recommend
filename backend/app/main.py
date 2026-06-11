from contextlib import asynccontextmanager
import logging
from threading import Thread

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.config import load_local_env

load_local_env()

from backend.app.api.routes import (
    close_viewing_history_record_service,
    prewarm_viewing_history_record_service,
    router,
    should_prewarm_record_selenium,
)

logger = logging.getLogger("uvicorn.error")


def _prewarm_record_selenium() -> None:
    try:
        prewarm_viewing_history_record_service()
        logger.info("Record Selenium driver prewarmed")
    except Exception:
        logger.exception("Record Selenium driver prewarm failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if should_prewarm_record_selenium():
        Thread(target=_prewarm_record_selenium, name="record-selenium-prewarm", daemon=True).start()
    try:
        yield
    finally:
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
