from fastapi import FastAPI

from backend.app.api.routes import router

app = FastAPI(title="Personal Movie Recommender")
app.include_router(router)
