"""Живой эндпоинт прогнозирования: POST /api/predict.

Модель живёт в выделенном потоке model-worker (см. app.services.prediction),
прогнозы UPSERT'ятся в таблицу forecasts через общую логику загрузки
(app.services.forecast_ingest), фронтенд читает их через существующий
GET /api/forecast. Схема БД не меняется.

При persist=true дополнительно пишется CSV в data/predictions/ в формате,
который принимает POST /api/ml/predict (upload-контракт).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime

from app.database import get_db
from app.models import Forecast, Route
from app.schemas import PredictRequest, PredictResponse, PredictForecastRow
from app.services.prediction import (
    MAX_ROWS_PER_REQUEST,
    count_requested_rows,
    model_worker,
)
from app.services.forecast_ingest import ingest_forecasts, write_predictions_csv

router = APIRouter(prefix="/api/predict", tags=["predict"])


@router.post("", response_model=PredictResponse)
async def predict(request: PredictRequest, db: AsyncSession = Depends(get_db)):
    """Сгенерировать прогноз пассажиропотока (реальная модель CatBoost или статистика).

    - `persist=true` (по умолчанию) — CSV в data/predictions/ + UPSERT в
      forecasts (повторные вызовы не создают дубликатов).
    - Ответ всегда содержит mode ("catboost"|"statistical") и сгенерированные
      строки, поэтому вызов с `persist=false` тоже отдаёт данные (dry-run:
      без CSV-файла и без записи в БД).
    """
    # ── 1. Маршруты: явный список или все маршруты ────────────────────────
    if request.route_ids:
        route_ids = list(dict.fromkeys(request.route_ids))
        existing = (
            await db.execute(select(Route.id).where(Route.id.in_(route_ids)))
        ).scalars().all()
        missing = set(route_ids) - set(existing)
        if missing:
            raise HTTPException(
                400,
                f"Маршруты с ID {sorted(missing)} не найдены в БД. Сначала запусти seed_data.py",
            )
    else:
        route_ids = (
            await db.execute(select(Route.id).order_by(Route.id))
        ).scalars().all()
        if not route_ids:
            raise HTTPException(400, "В базе нет маршрутов для прогнозирования")

    routes = (
        await db.execute(select(Route).where(Route.id.in_(route_ids)).order_by(Route.id))
    ).scalars().all()
    route_infos = [
        {"id": r.id, "number": r.number, "index": i} for i, r in enumerate(routes)
    ]

    # ── 2. Начало прогноза (по умолчанию — текущий час сервера) ───────────
    start = request.start or datetime.now().replace(minute=0, second=0, microsecond=0)

    # ── 3. Лимит генерируемых строк (routes × hours ≤ 100_000) ────────────
    total = count_requested_rows(len(route_infos), start, request.horizon, request.periods)
    if total > MAX_ROWS_PER_REQUEST:
        raise HTTPException(
            400,
            f"Слишком много строк прогноза: {total}. Максимум {MAX_ROWS_PER_REQUEST} "
            f"(маршрутов × часов). Уменьшите periods.",
        )

    # ── 4. Базовая нагрузка маршрутов из существующих прогнозов ───────────
    agg = (
        await db.execute(
            select(Forecast.route_id, func.avg(Forecast.passengers_predicted))
            .where(Forecast.route_id.in_(route_ids))
            .group_by(Forecast.route_id)
        )
    ).all()
    base_loads = {rid: float(avg) for rid, avg in agg}

    # ── 5. Прогноз в потоке модели (event loop не блокируется) ────────────
    result = await model_worker.predict(
        route_infos, start, request.horizon, request.periods, base_loads
    )
    rows = result["rows"]

    # ── 6. Persist: CSV в data/predictions/ + общая логика загрузки в БД ──
    csv_path = None
    stored = 0
    if request.persist and rows:
        csv_path = str(write_predictions_csv(rows))
        try:
            stored = await ingest_forecasts(db, rows)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    return PredictResponse(
        mode=result["mode"],
        model_path=result.get("model_path"),
        csv_path=csv_path,
        predicted=len(rows),
        stored=stored,
        rows=[PredictForecastRow(**row) for row in rows],
    )