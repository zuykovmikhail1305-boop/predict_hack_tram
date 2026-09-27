"""API-ручки для загрузки прогнозов ML-модели Миши."""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import csv, io, json

from app.database import get_db
from app.models import Forecast
from app.services.forecast_ingest import ingest_forecasts

router = APIRouter(prefix="/api/ml", tags=["ml"])


@router.post("/predict")
async def upload_ml_predictions(
    file: UploadFile = File(..., description="CSV файл с прогнозами"),
    db: AsyncSession = Depends(get_db),
):
    """
    Загрузить прогнозы ML-модели в БД.
    
    Формат CSV (обязательные колонки):
      route_id,timestamp,hour,passengers_predicted
    Опционально:
      passengers_lower,passengers_upper
    
    Пример:
      route_id,timestamp,hour,passengers_predicted,passengers_lower,passengers_upper
      1,2025-11-01,7,245,196,294
      1,2025-11-01,8,312,250,374
    """
    if not file.filename or not file.filename.endswith(('.csv', '.json')):
        raise HTTPException(400, "Поддерживаются только .csv и .json файлы")
    
    content = await file.read()
    text = content.decode("utf-8-sig")

    records = []
    
    if file.filename.endswith('.csv'):
        reader = csv.DictReader(io.StringIO(text))
        required = {"route_id", "timestamp", "hour", "passengers_predicted"}
        if not required.issubset(reader.fieldnames or []):
            raise HTTPException(400, f"CSV должен содержать колонки: {required}")
        
        for row in reader:
            try:
                records.append({
                    "route_id": int(row["route_id"]),
                    "timestamp": datetime.strptime(row["timestamp"].strip(), "%Y-%m-%d"),
                    "hour": int(row["hour"]),
                    "passengers_predicted": int(row["passengers_predicted"]),
                    "passengers_lower": int(row["passengers_lower"]) if row.get("passengers_lower") else None,
                    "passengers_upper": int(row["passengers_upper"]) if row.get("passengers_upper") else None,
                })
            except (ValueError, KeyError) as e:
                raise HTTPException(400, f"Ошибка в строке {row}: {e}")
    
    elif file.filename.endswith('.json'):
        data = json.loads(text)
        if isinstance(data, dict):
            data = [data]
        for row in data:
            try:
                records.append({
                    "route_id": int(row["route_id"]),
                    "timestamp": datetime.strptime(row["timestamp"].strip(), "%Y-%m-%d"),
                    "hour": int(row["hour"]),
                    "passengers_predicted": int(row["passengers_predicted"]),
                    "passengers_lower": int(row.get("passengers_lower")),
                    "passengers_upper": int(row.get("passengers_upper")),
                })
            except (ValueError, KeyError) as e:
                raise HTTPException(400, f"Ошибка в записи {row}: {e}")
    
    if not records:
        raise HTTPException(400, "Нет записей для загрузки")
    
    route_ids = set(r["route_id"] for r in records)
    
    # Валидация маршрутов + UPSERT-вставка — общая логика с POST /api/predict
    try:
        loaded = await ingest_forecasts(db, records)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    
    return {
        "status": "ok",
        "loaded": loaded,
        "routes": sorted(route_ids),
        "message": f"✅ Загружено {loaded} прогнозов для {len(route_ids)} маршрутов",
    }


@router.delete("/predict")
async def clear_ml_predictions(db: AsyncSession = Depends(get_db)):
    """Очистить все прогнозы из БД (для перезагрузки новой моделью)."""
    from sqlalchemy import delete
    await db.execute(delete(Forecast))
    await db.commit()
    return {"status": "ok", "message": "🧹 Все прогнозы очищены"}


@router.get("/predict/stats")
async def get_ml_stats(db: AsyncSession = Depends(get_db)):
    """Статистика загруженных прогнозов."""
    from sqlalchemy import func
    total = (await db.execute(select(func.count(Forecast.id)))).scalar()
    routes = (await db.execute(select(Forecast.route_id).distinct())).scalars().all()
    dates = (await db.execute(
        select(func.min(Forecast.timestamp), func.max(Forecast.timestamp))
    )).first()
    return {
        "total_forecasts": total,
        "routes_covered": len(routes),
        "date_range": f"{dates[0]} — {dates[1]}" if dates[0] else "нет данных",
    }