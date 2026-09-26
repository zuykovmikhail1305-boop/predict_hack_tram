import asyncio
import sys
import os

# Путь до корня проекта
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from app.database import init_db, async_session
from app.models import Route, Stop, Forecast, Scenario
from datetime import datetime, timedelta
import random


async def seed():
    await init_db()
    async with async_session() as session:
        # Маршруты (10 штук из данных)
        routes_data = [
            (1, "Калужская — Тверская Застава"),
            (5, "Новокузнецкая — Метро «Университет»"),
            (7, "Калужская — Новокузнецкая"),
            (11, "Останкино — Метро «Тимирязевская»"),
            (12, "Метро «Тимирязевская» — Останкино"),
            (13, "Новокузнецкая — Тверская Застава"),
            (15, "Калужская — Метро «Университет»"),
            (17, "Тверская Застава — Новокузнецкая"),
            (20, "Метро «Тимирязевская» — Калужская"),
            (25, "Останкино — Тверская Застава"),
        ]

        for num, name in routes_data:
            route = Route(number=num, name=name)
            session.add(route)
        await session.flush()

        # Остановки (несколько на маршрут — координаты в центре Москвы)
        stops_names = [
            "Тверская Застава", "Белорусский вокзал", "Метро «Маяковская»",
            "Пушкинская площадь", "Трубная площадь", "Чистые пруды",
            "Курский вокзал", "Таганская площадь", "Павелецкий вокзал",
            "Метро «Новокузнецкая»", "Метро «Университет»", "Калужская",
            "Останкино", "Метро «Тимирязевская»", "Дмитровская",
            "Менделеевская", "Новослободская", "Савеловский вокзал",
        ]

        # Координаты остановок (случайное смещение от центра Москвы)
        base_lat, base_lon = 55.7558, 37.6176
        for i, r in enumerate(routes_data):
            route_num, route_name = r
            route = await session.get(Route, i + 1)
            # Берём 4-6 остановок на маршрут
            n_stops = random.randint(4, 6)
            selected = random.sample(stops_names, n_stops)
            for j, name in enumerate(selected):
                stop = Stop(
                    route_id=route.id,
                    name=name,
                    lat=base_lat + random.uniform(-0.05, 0.05),
                    lon=base_lon + random.uniform(-0.05, 0.05),
                    order_num=j + 1,
                    is_terminal=(j == 0 or j == n_stops - 1),
                )
                session.add(stop)
        await session.flush()

        # Прогнозы — ноябрь-декабрь 2025, почасовые
        start = datetime(2025, 11, 1)
        end = datetime(2025, 12, 31, 23)
        current = start
        routes = await session.execute(__import__("sqlalchemy").select(Route))
        routes = routes.scalars().all()

        while current <= end:
            for route in routes:
                base = random.randint(50, 300)
                hour = current.hour
                # Утренний пик ~7-9, вечерний ~17-19
                if 7 <= hour <= 9:
                    multiplier = random.uniform(1.5, 2.0)
                elif 17 <= hour <= 19:
                    multiplier = random.uniform(1.5, 2.0)
                elif 0 <= hour <= 5:
                    multiplier = random.uniform(0.0, 0.1)  # Ночь, почти 0
                elif 10 <= hour <= 16:
                    multiplier = random.uniform(0.8, 1.2)
                else:
                    multiplier = random.uniform(0.5, 0.8)

                # Выходные и праздники — меньше
                is_weekend = current.weekday() >= 5
                if is_weekend:
                    multiplier *= random.uniform(0.5, 0.7)

                passengers = max(0, int(base * multiplier))

                forecast = Forecast(
                    route_id=route.id,
                    timestamp=current,
                    hour=hour,
                    passengers_predicted=passengers,
                    passengers_lower=max(0, int(passengers * 0.8)),
                    passengers_upper=int(passengers * 1.2),
                    is_weekend=is_weekend,
                    is_holiday=False,
                )
                session.add(forecast)

                # Прогресс
                if (current - start).days % 7 == 0 and hour == 0:
                    print(f"  {current.date()} - добавлено", flush=True)

            current += timedelta(hours=1)

        await session.commit()
        print(f"✅ Сид завершён! Добавлено {len(routes)} маршрутов и прогнозы на ноябрь-декабрь 2025")


if __name__ == "__main__":
    print("🌱 Сидирование данных...")
    asyncio.run(seed())