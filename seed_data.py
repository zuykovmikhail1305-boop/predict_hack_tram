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
        # Маршруты (10 штук) — реальные координаты из JSON
        import json
        routes_path = os.path.join(os.path.dirname(__file__), "backend", "data", "moscow_tram_routes.json")
        with open(routes_path, encoding="utf-8") as f:
            routes_geo = json.load(f)["routes"]

        for num_str, route_info in routes_geo.items():
            num = int(num_str)
            route = Route(number=num, name=route_info["name"])
            session.add(route)
        await session.flush()

        # Остановки — реальные координаты из JSON
        for i, (num_str, route_info) in enumerate(routes_geo.items()):
            route = await session.get(Route, i + 1)
            for stop_data in route_info["stops"]:
                stop = Stop(
                    route_id=route.id,
                    name=stop_data["name"],
                    lat=stop_data["lat"],
                    lon=stop_data["lon"],
                    order_num=stop_data["order"],
                    is_terminal=stop_data["is_terminal"],
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