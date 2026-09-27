"""Общая логика персиста прогнозов в таблицу forecasts.

Извлечена из обработчика POST /api/ml/predict (ml_router.py) и используется
также живым эндпоинтом POST /api/predict — код один, дублирования нет:

- ingest_forecasts(db, records) — валидация route_id по таблице routes +
  bulk-insert в forecasts с UPSERT-семантикой (повторная загрузка тех же
  ключей (route_id, timestamp, hour) не создаёт дубликатов);
- write_predictions_csv(rows) — запись CSV в data/predictions/ в формате,
  который принимает POST /api/ml/predict (upload-контракт).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Forecast, Route

# Каталог data/predictions относительно пакета backend (как data/ в database.py).
PREDICTIONS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "predictions"

CSV_COLUMNS = [
    "route_id", "timestamp", "hour", "passengers_predicted",
    "passengers_lower", "passengers_upper",
]

_SQLITE_IN_LIMIT = 400


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def ingest_forecasts(
    db: AsyncSession,
    records: list[dict],
    commit: bool = True,
) -> int:
    """Проверить маршруты и загрузить записи в forecasts (UPSERT по ключу).

    Параметры
    ---------
    records: список диктов с полями
        route_id, timestamp (datetime/date), hour, passengers_predicted
        [+ passengers_lower, passengers_upper].
    commit:  коммитить транзакцию здесь (иначе — на усмотрение вызывающего).

    Возвращает число загруженных записей.

    Исключения
    ----------
    ValueError — с русским сообщением (в стиле существующих HTTPException),
        если records пуст или есть неизвестные route_id.
    """
    if not records:
        raise ValueError("Нет записей для загрузки")

    route_ids = set(r["route_id"] for r in records)
    existing = (
        await db.execute(select(Route.id).where(Route.id.in_(route_ids)))
    ).scalars().all()
    missing = route_ids - set(existing)
    if missing:
        raise ValueError(
            f"Маршруты с ID {missing} не найдены в БД. Сначала запусти seed_data.py"
        )

    forecast_objects = []
    for rec in records:
        ts = rec["timestamp"]
        forecast_objects.append(Forecast(
            route_id=rec["route_id"],
            timestamp=ts,
            hour=rec["hour"],
            passengers_predicted=rec["passengers_predicted"],
            passengers_lower=rec.get("passengers_lower"),
            passengers_upper=rec.get("passengers_upper"),
            is_weekend=ts.weekday() >= 5,
        ))

    # UPSERT-семантика: удаляем только строки с совпадающим ключом
    # (route_id, timestamp, hour) — чужие записи не трогаем.
    keys = list({(r["route_id"], r["timestamp"], r["hour"]) for r in records})
    ids_to_delete: list[int] = []
    for chunk in _chunks(keys, _SQLITE_IN_LIMIT):
        condition = or_(*[
            and_(
                Forecast.route_id == rid,
                Forecast.timestamp == ts,
                Forecast.hour == hour,
            )
            for rid, ts, hour in chunk
        ])
        found = (
            await db.execute(select(Forecast.id).where(condition))
        ).scalars().all()
        ids_to_delete.extend(found)
    for chunk in _chunks(ids_to_delete, _SQLITE_IN_LIMIT):
        await db.execute(delete(Forecast).where(Forecast.id.in_(chunk)))

    db.add_all(forecast_objects)
    if commit:
        await db.commit()
    return len(forecast_objects)


def predictions_dir() -> Path:
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return PREDICTIONS_DIR


def write_predictions_csv(rows: list[dict]) -> Path:
    """Записать прогнозы в data/predictions/predictions_YYYYMMDD_HHMMSS.csv.

    Формат — ровно upload-контракт POST /api/ml/predict:
    route_id,timestamp(%Y-%m-%d),hour,passengers_predicted[,lower,upper].
    Возвращает путь к созданному файлу.
    """
    import csv as _csv

    out_dir = predictions_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"predictions_{stamp}.csv"
    counter = 1
    while path.exists():  # защита от коллизий при нескольких вызовах в секунду
        path = out_dir / f"predictions_{stamp}_{counter}.csv"
        counter += 1

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = _csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            ts = row["timestamp"]
            writer.writerow({
                "route_id": row["route_id"],
                "timestamp": ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts),
                "hour": row["hour"],
                "passengers_predicted": row["passengers_predicted"],
                "passengers_lower": row.get("passengers_lower"),
                "passengers_upper": row.get("passengers_upper"),
            })
    return path