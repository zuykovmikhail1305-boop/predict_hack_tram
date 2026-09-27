"""API-ручки для внешних данных (погода, календарь, события)."""
from fastapi import APIRouter, Query, HTTPException
from typing import Optional

from app.external_data import (
    get_weather, get_calendar, get_combined, get_batch,
    get_school_holidays, get_special_events, export_to_csv,
)
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/external", tags=["external"])


@router.get("/weather")
async def api_weather(date: str = Query(..., description="Дата YYYY-MM-DD"), hour: Optional[int] = None):
    """🌤️ Погода по дате/часу (Open-Meteo, бесплатно)."""
    try:
        return get_weather(date, hour=hour)
    except Exception as e:
        raise HTTPException(502, f"Ошибка погодного API: {e}")


@router.get("/calendar")
async def api_calendar(date: str = Query(..., description="Дата YYYY-MM-DD")):
    """📅 Производственный календарь РФ (isdayoff.ru, бесплатно)."""
    try:
        return get_calendar(date)
    except Exception as e:
        raise HTTPException(502, f"Ошибка календаря: {e}")


@router.get("/combined")
async def api_combined(date: str = Query(..., description="Дата YYYY-MM-DD"), hour: Optional[int] = None):
    """🌤️📅 Погода + календарь за один запрос."""
    try:
        return get_combined(date, hour=hour)
    except Exception as e:
        raise HTTPException(502, f"Ошибка комбинированного запроса: {e}")


@router.post("/batch")
async def api_batch(dates: list[str], hour: Optional[int] = None):
    """
    🔄 Массовый запрос — передай массив дат, получи массив результатов.
    Всё параллельно, быстро.

    Пример тела:
    {"dates": ["2025-11-01", "2025-11-04", "2025-12-31"], "hour": 12}
    """
    if len(dates) > 100:
        raise HTTPException(400, "Максимум 100 дат за раз")
    try:
        return get_batch(dates, hour=hour)
    except Exception as e:
        raise HTTPException(502, f"Ошибка массового запроса: {e}")


@router.get("/school-holidays")
async def api_school_holidays(year: int = 2025):
    """🏫 Школьные каникулы Москвы на год."""
    return get_school_holidays(year)


@router.get("/events")
async def api_events():
    """🎭 Известные события Москвы (ноябрь-декабрь 2025)."""
    return get_special_events()


@router.get("/export-csv", response_class=PlainTextResponse)
async def api_export_csv(
    dates: str = Query(..., description="Даты через запятую: 2025-11-01,2025-11-02"),
    hour: Optional[int] = None,
):
    """
    📊 CSV для таблички.
    Передай даты строкой через запятую.
    """
    date_list = [d.strip() for d in dates.split(",") if d.strip()]
    if not date_list:
        raise HTTPException(400, "Укажи хотя бы одну дату")
    try:
        return export_to_csv(date_list, hour=hour)
    except Exception as e:
        raise HTTPException(502, f"Ошибка генерации CSV: {e}")