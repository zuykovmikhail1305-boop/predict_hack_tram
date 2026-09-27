"""API-ручки для загрузки прогнозов ML-модели Миши."""
from fastapi import APIRouter, Depends,HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
import csv, io, json

from app.database import get_db, async_session
from app.models import Forecast, Route

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
    
    # Валидация маршрутов
    route_ids = set(r["route_id"] for r in records)
    existing = (await db.execute(select(Route.id).where(Route.id.in_(route_ids)))).scalars().all()
    missing = route_ids - set(existing)
    if missing:
        raise HTTPException(400, f"Маршруты с ID {missing} не найдены в БД. Сначала запусти seed_data.py")
    
    # Вставка записей
    forecast_objects = []
    for rec in records:
        forecast_objects.append(Forecast(
            route_id=rec["route_id"],
            timestamp=rec["timestamp"],
            hour=rec["hour"],
            passengers_predicted=rec["passengers_predicted"],
            passengers_lower=rec["passengers_lower"],
            passengers_upper=rec["passengers_upper"],
            is_weekend=rec["timestamp"].weekday() >= 5,
        ))
    
    db.add_all(forecast_objects)
    await db.commit()
    
    return {
        "status": "ok",
        "loaded": len(records),
        "routes": sorted(route_ids),
        "message": f"✅ Загружено {len(records)} прогнозов для {len(route_ids)} маршрутов",
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