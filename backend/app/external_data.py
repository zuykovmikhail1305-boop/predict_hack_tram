"""
Сбор внешних данных из бесплатных API для прогноза пассажиропотока.
Источники:
- Open-Meteo Historical API (погода, без ключа)
- isdayoff.ru (производственный календарь РФ)
- Статические справочники (каникулы, события)

Использование:
    from app.external_data import get_weather, get_calendar, get_holidays_batch

    # Одиночный запрос
    w = get_weather("2025-11-01", hour=8)
    c = get_calendar("2025-11-04")

    # Массив
    results = get_batch(["2025-11-01", "2025-11-02"])
"""

import asyncio
import json
import os
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import quote

import httpx

# ─── Конфигурация ────────────────────────────────────────────────
MOSCOW_LAT = 55.7558
MOSCOW_LON = 37.6176

# ─── 1. Погода (Open-Meteo Historical API) ──────────────────────

async def get_weather_async(dt: str, hour: Optional[int] = None) -> dict:
    """
    Погода по дате/часу через Open-Meteo.
    Бесплатно, без API-ключа.
    https://open-meteo.com/

    >>> get_weather("2025-11-15", hour=8)
    {"date": "2025-11-15", "hour": 8, "temperature": -2.3, "precipitation": 0.5, ...}
    """
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": MOSCOW_LAT,
        "longitude": MOSCOW_LON,
        "start_date": dt,
        "end_date": dt,
        "timezone": "Europe/Moscow",
        "hourly": [
            "temperature_2m",
            "precipitation",
            "snowfall",
            "cloud_cover",
            "wind_speed_10m",
            "surface_pressure",
        ],
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    result = {
        "date": dt,
        "hour": hour,
        "source": "open-meteo",
        "hourly_data": {},
    }

    for i, t in enumerate(times):
        h = int(t.split("T")[1].split(":")[0])
        if hour is not None and h != hour:
            continue
        result["hourly_data"][h] = {
            "temperature": hourly.get("temperature_2m", [None])[i],
            "precipitation": hourly.get("precipitation", [None])[i],
            "snowfall": hourly.get("snowfall", [None])[i],
            "cloud_cover": hourly.get("cloud_cover", [None])[i],
            "wind_speed": hourly.get("wind_speed_10m", [None])[i],
            "pressure": hourly.get("surface_pressure", [None])[i],
        }

    if hour is not None and hour in result["hourly_data"]:
        return {
            "date": dt,
            "hour": hour,
            "source": "open-meteo",
            **result["hourly_data"][hour],
        }

    return result


def get_weather(dt: str, hour: Optional[int] = None) -> dict:
    """Синхронная обёртка для get_weather_async."""
    return asyncio.run(get_weather_async(dt, hour))


# ─── 2. Производственный календарь (isdayoff.ru) ────────────────

async def get_calendar_async(dt: str, year: Optional[int] = None) -> dict:
    """
    Проверка дня: рабочий/выходной/праздничный через isdayoff.ru
    Бесплатно, без ключа.
    Результаты кэшируются на уровне API.

    >>> get_calendar("2025-11-04")
    {"date": "2025-11-04", "is_holiday": true, "day_type": "holiday", ...}
    """
    d = datetime.strptime(dt, "%Y-%m-%d")
    y = year or d.year
    url = f"https://isdayoff.ru/api/getdata?year={y}&month={d.month}&day={d.day}&cc=ru&delimiter=,"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        raw = resp.text.strip()

    # API возвращает 0=рабочий, 1=выходной, 2=праздник, 4=предпраздничный (сокращённый)
    day_code = int(raw)

    day_type_map = {
        0: "working",
        1: "day_off",
        2: "holiday",
        4: "preholiday",
    }

    # Определяем дополнительно
    dow = d.weekday()
    is_weekend = dow >= 5
    is_holiday = day_code in (1, 2)
    is_preholiday = day_code == 4
    is_working_saturday = day_code == 0 and dow == 5  # рабочая суббота

    return {
        "date": dt,
        "source": "isdayoff.ru",
        "day_code": day_code,
        "day_type": day_type_map.get(day_code, "unknown"),
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "is_preholiday": is_preholiday,
        "is_working_saturday": is_working_saturday,
        "weekday": dow,
        "weekday_name": [
            "пн", "вт", "ср", "чт", "пт", "сб", "вс"
        ][dow],
    }


def get_calendar(dt: str, year: Optional[int] = None) -> dict:
    """Синхронная обёртка."""
    return asyncio.run(get_calendar_async(dt, year))


# ─── 3. Погода + Календарь в одном запросе ─────────────────────

async def get_combined_async(dt: str, hour: Optional[int] = None) -> dict:
    """Погода + календарь для одной даты (и часа)."""
    weather_task = get_weather_async(dt, hour=hour)
    calendar_task = get_calendar_async(dt)
    weather, calendar = await asyncio.gather(weather_task, calendar_task)
    return {**calendar, "weather": weather}


def get_combined(dt: str, hour: Optional[int] = None) -> dict:
    """Синхронная обёртка."""
    return asyncio.run(get_combined_async(dt, hour))


# ─── 4. Массовая обработка (массив дат) ─────────────────────────

async def get_batch_async(dates: list[str], hour: Optional[int] = None) -> list[dict]:
    """
    Получить данные для массива дат.
    Все запросы выполняются параллельно — быстро.
    """
    tasks = [get_combined_async(d, hour=hour) for d in dates]
    return await asyncio.gather(*tasks)


def get_batch(dates: list[str], hour: Optional[int] = None) -> list[dict]:
    """Синхронная обёртка."""
    return asyncio.run(get_batch_async(dates, hour))


# ─── 5. Статические справочники ─────────────────────────────────

def get_school_holidays(year: int = 2025) -> list[dict]:
    """
    Школьные каникулы. Данные из открытых источников Москвы.
    """
    holidays_map = {
        "осенние": ("2025-10-25", "2025-11-02"),
        "зимние": ("2025-12-31", "2026-01-11"),
        "весенние": ("2026-03-22", "2026-03-30"),
    }
    return [
        {"name": name, "start": start, "end": end}
        for name, (start, end) in holidays_map.items()
        if int(start[:4]) == year or int(end[:4]) == year
    ]


def get_special_events() -> list[dict]:
    """
    Известные события Москвы на ноябрь-декабрь 2025.
    Источник: открытые календари (mos.ru, спорт, концерты).
    """
    return [
        {"date": "2025-11-03", "event": "День народного единства (продолжение)", "type": "holiday"},
        {"date": "2025-11-04", "event": "День народного единства", "type": "holiday"},
        {"date": "2025-12-26", "event": "Сильный снегопад (исторические данные)", "type": "weather_event"},
        {"date": "2025-12-31", "event": "Новый год — выходной день", "type": "holiday"},
        {"date": "2025-11-01", "event": "Суббота — рабочий сокращённый день (перенос с 05.01)", "type": "preholiday"},
    ]


# ─── 6. Экспорт в CSV для таблички ──────────────────────────────

def export_to_csv(dates: list[str], hour: Optional[int] = None) -> str:
    """
    Формирует CSV-строку с данными для вставки в табличку.
    """
    data = get_batch(dates, hour=hour)
    lines = []
    lines.append("date,hour,day_type,is_weekend,is_holiday,is_preholiday,temperature,precipitation,snowfall,cloud_cover,wind_speed")

    for row in data:
        weather = row.get("weather", {})
        date_str = row["date"]
        h = weather.get("hour", hour) if isinstance(weather, dict) else hour
        lines.append(
            f"{date_str},{h or ''},"
            f"{row.get('day_type','')},{row.get('is_weekend',0)},{row.get('is_holiday',0)},{row.get('is_preholiday',0)},"
            f"{weather.get('temperature','') if isinstance(weather, dict) else ''},"
            f"{weather.get('precipitation','') if isinstance(weather, dict) else ''},"
            f"{weather.get('snowfall','') if isinstance(weather, dict) else ''},"
            f"{weather.get('cloud_cover','') if isinstance(weather, dict) else ''},"
            f"{weather.get('wind_speed','') if isinstance(weather, dict) else ''}"
        )

    return "\n".join(lines)


# ─── Тест при запуске ────────────────────────────────────────────
if __name__ == "__main__":
    print("=== 🌤️  Погода (одиночный запрос) ===")
    w = get_weather("2025-11-15", hour=8)
    print(json.dumps(w, indent=2, ensure_ascii=False))

    print("\n=== 📅 Календарь (одиночный запрос) ===")
    c = get_calendar("2025-11-04")
    print(json.dumps(c, indent=2, ensure_ascii=False))

    print("\n=== 🔄 Массовый запрос (3 даты) ===")
    batch = get_batch(["2025-11-01", "2025-11-04", "2025-12-31"], hour=12)
    for b in batch:
        print(f"  {b['date']}: {b['day_type']}, temp={b.get('weather', {}).get('temperature', 'N/A')}")

    print("\n=== 🏫 Каникулы ===")
    print(json.dumps(get_school_holidays(), indent=2, ensure_ascii=False))

    print("\n=== 📊 CSV для таблички ===")
    csv_data = export_to_csv(["2025-11-01", "2025-11-02", "2025-11-03", "2025-11-04"])
    print(csv_data[:500] + ("..." if len(csv_data) > 500 else ""))