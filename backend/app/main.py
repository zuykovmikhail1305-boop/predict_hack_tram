from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.database import init_db
from app.routers import routes_router, stops_router, forecast_router, export_router, external_router, ml_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Трамваи — Прогноз пассажиропотока",
    description="Веб-сервис для ИИ-прогнозирования загрузки трамвайных маршрутов",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_router.router)
app.include_router(stops_router.router)
app.include_router(forecast_router.router)
app.include_router(export_router.router)
app.include_router(external_router.router)
app.include_router(ml_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}