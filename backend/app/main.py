from fastapi import FastAPI

from backend.app.config import load_local_env

load_local_env()

from backend.app.api.routes import router

app = FastAPI(title="Personal Movie Recommender")
app.include_router(router)
