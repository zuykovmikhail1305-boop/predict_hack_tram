"""Тесты для FastAPI эндпоинтов."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Route, Stop, Forecast, Scenario
from app.services.prediction import model_worker

# Используем in-memory SQLite для тестов
TEST_DATABASE_URL = "sqlite+aiosqlite://"

@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine):
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_local() as s:
        yield s


@pytest_asyncio.fixture
async def client(session):
    """Тестовый HTTP клиент с переопределённой БД."""

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def seed_data(session):
    """Заполняем тестовую БД."""
    # Маршруты
    routes = [
        Route(number=1, name="Калужская — Тверская Застава"),
        Route(number=5, name="Новокузнецкая — Университет"),
        Route(number=7, name="Калужская — Новокузнецкая"),
    ]
    session.add_all(routes)
    await session.flush()

    # Остановки
    stops = [
        Stop(route_id=1, name="Тверская Застава", lat=55.7765, lon=37.5820, order_num=1, is_terminal=True),
        Stop(route_id=1, name="Белорусский вокзал", lat=55.7763, lon=37.5812, order_num=2),
        Stop(route_id=1, name="Пушкинская площадь", lat=55.7655, lon=37.6055, order_num=3, is_terminal=True),
        Stop(route_id=5, name="Метро Университет", lat=55.7500, lon=37.5340, order_num=1, is_terminal=True),
        Stop(route_id=7, name="Калужская", lat=55.7350, lon=37.6200, order_num=1, is_terminal=True),
    ]
    session.add_all(stops)
    await session.flush()

    # Прогнозы
    from datetime import datetime
    forecasts = [
        Forecast(route_id=1, timestamp=datetime(2025, 11, 1), hour=8, passengers_predicted=245, passengers_lower=196, passengers_upper=294),
        Forecast(route_id=1, timestamp=datetime(2025, 11, 1), hour=9, passengers_predicted=312, passengers_lower=250, passengers_upper=374),
        Forecast(route_id=1, timestamp=datetime(2025, 11, 1), hour=10, passengers_predicted=180, passengers_lower=144, passengers_upper=216),
        Forecast(route_id=5, timestamp=datetime(2025, 11, 1), hour=8, passengers_predicted=150, passengers_lower=120, passengers_upper=180),
    ]
    session.add_all(forecasts)
    await session.commit()


# ======================= ТЕСТЫ =======================

class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestRoutes:
    async def test_get_routes(self, client, seed_data):
        resp = await client.get("/api/routes")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 3
        # Проверяем структуру
        assert data[0]["number"] in (1, 5, 7)
        assert "name" in data[0]
        assert "id" in data[0]

    async def test_routes_empty(self, client):
        """Без seed_data — пустой список."""
        resp = await client.get("/api/routes")
        assert resp.status_code == 200
        assert resp.json() == []


class TestStops:
    async def test_get_all_stops(self, client, seed_data):
        resp = await client.get("/api/stops")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 5

    async def test_get_stops_by_route(self, client, seed_data):
        resp = await client.get("/api/stops", params={"route": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3  # 3 остановки у маршрута 1
        assert all(s["route_id"] == 1 for s in data)

    async def test_stops_invalid_route(self, client, seed_data):
        resp = await client.get("/api/stops", params={"route": 999})
        assert resp.status_code == 200
        assert resp.json() == []


class TestForecast:
    async def test_get_forecast_all(self, client, seed_data):
        resp = await client.get("/api/forecast")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 4

    async def test_get_forecast_by_route(self, client, seed_data):
        resp = await client.get("/api/forecast", params={"route": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        for f in data:
            assert f["route_number"] == 1

    async def test_forecast_by_date_range(self, client, seed_data):
        resp = await client.get("/api/forecast", params={
            "route": 1, "from_date": "2025-11-01", "to_date": "2025-11-01",
        })
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_forecast_daily_granularity(self, client, seed_data):
        resp = await client.get("/api/forecast", params={
            "route": 1, "granularity": "day",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        # Дневная аггрегация — hour == 0
        for f in data:
            assert f["hour"] == 0

    async def test_forecast_invalid_dates(self, client, seed_data):
        resp = await client.get("/api/forecast", params={
            "from_date": "недата",
        })
        assert resp.status_code == 400

    async def test_forecast_no_data(self, client, seed_data):
        resp = await client.get("/api/forecast", params={
            "route": 999,
        })
        assert resp.status_code == 200
        assert resp.json() == []


class TestScenario:
    async def test_create_scenario(self, client, seed_data):
        resp = await client.post("/api/forecast/scenario", json={
            "weather_factor": 0.9,
            "event_factor": 1.1,
            "season_factor": 1.0,
            "name": "Дождливый день",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["weather_factor"] == 0.9
        assert data["event_factor"] == 1.1
        assert data["id"] is not None

    async def test_create_scenario_bad_values(self, client, seed_data):
        """Коэффициенты должны быть в диапазоне 0-3."""
        resp = await client.post("/api/forecast/scenario", json={
            "weather_factor": 5.0,
            "event_factor": 1.0,
            "season_factor": 1.0,
        })
        assert resp.status_code == 422  # Pydantic validation error

    async def test_list_scenarios(self, client, seed_data):
        # Сначала создадим
        await client.post("/api/forecast/scenario", json={
            "weather_factor": 0.8, "event_factor": 1.2, "season_factor": 1.0,
        })
        resp = await client.get("/api/forecast/scenarios")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert "weather_factor" in data[0]

    async def test_delete_scenario(self, client, seed_data):
        # Создаём
        create = await client.post("/api/forecast/scenario", json={
            "weather_factor": 1.0, "event_factor": 1.0, "season_factor": 1.0,
        })
        scenario_id = create.json()["id"]

        # Удаляем
        resp = await client.delete(f"/api/forecast/scenario/{scenario_id}")
        assert resp.status_code == 200

        # Проверяем что удалился
        resp = await client.delete(f"/api/forecast/scenario/{scenario_id}")
        assert resp.status_code == 404


class TestExport:
    async def test_export_csv(self, client, seed_data):
        resp = await client.get("/api/forecast/export", params={"format": "csv"})
        assert resp.status_code == 200
        text = resp.text
        assert "route_id" in text
        assert "passengers_predicted" in text
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    async def test_export_xlsx(self, client, seed_data):
        resp = await client.get("/api/forecast/export", params={"format": "xlsx"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    async def test_export_by_route(self, client, seed_data):
        resp = await client.get("/api/forecast/export", params={
            "route": 1, "format": "csv",
        })
        assert resp.status_code == 200
        # Только данные маршрута 1 (3 записи)
        lines = resp.text.strip().split("\n")
        assert len(lines) == 4  # header + 3 rows


class TestExternalData:
    """Тесты для внешних данных (calendar, holidays, events)."""

    async def test_calendar_holiday(self, client, seed_data):
        """4 ноября — праздник."""
        resp = await client.get("/api/external/calendar", params={"date": "2025-11-04"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_holiday"] == True
        assert data["day_type"] == "holiday"

    async def test_calendar_weekend(self, client, seed_data):
        """Воскресенье — выходной."""
        resp = await client.get("/api/external/calendar", params={"date": "2025-11-02"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_weekend"] == True

    async def test_calendar_working(self, client, seed_data):
        """Будний день — рабочий."""
        resp = await client.get("/api/external/calendar", params={"date": "2025-11-03"})
        assert resp.status_code == 200
        data = resp.json()
        # 3 ноября — предпраздничный или рабочий
        assert "day_type" in data

    async def test_calendar_invalid_date(self, client, seed_data):
        resp = await client.get("/api/external/calendar", params={"date": "invalid"})
        assert resp.status_code == 502  # ошибка API

    async def test_school_holidays(self, client, seed_data):
        resp = await client.get("/api/external/school-holidays", params={"year": 2025})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        assert "name" in data[0]
        assert "start" in data[0]

    async def test_events(self, client, seed_data):
        resp = await client.get("/api/external/events")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        assert "date" in data[0]
        assert "event" in data[0]

    async def test_batch(self, client, seed_data):
        resp = await client.post("/api/external/batch", json={
            "dates": ["2025-11-01", "2025-11-04"],
            "hour": 12,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["date"] == "2025-11-01"
        assert data[1]["date"] == "2025-11-04"

    async def test_batch_too_many(self, client, seed_data):
        resp = await client.post("/api/external/batch", json={
            "dates": [f"2025-01-{d:02d}" for d in range(1, 101)],
        })
        assert resp.status_code == 400  # максимум 100


class TestML:
    async def test_ml_predict_upload_csv(self, client, seed_data):
        csv_data = "route_id,timestamp,hour,passengers_predicted\n1,2025-12-01,8,300\n1,2025-12-01,9,350"
        resp = await client.post(
            "/api/ml/predict",
            files={"file": ("test.csv", csv_data, "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["loaded"] == 2

    async def test_ml_predict_empty_csv(self, client, seed_data):
        resp = await client.post(
            "/api/ml/predict",
            files={"file": ("empty.csv", b"", "text/csv")},
        )
        assert resp.status_code == 400

    async def test_ml_predict_bad_route(self, client, seed_data):
        csv_data = "route_id,timestamp,hour,passengers_predicted\n999,2025-12-01,8,300"
        resp = await client.post(
            "/api/ml/predict",
            files={"file": ("bad.csv", csv_data, "text/csv")},
        )
        assert resp.status_code == 400  # маршрут не найден

    async def test_ml_stats(self, client, seed_data):
        resp = await client.get("/api/ml/predict/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_forecasts" in data
        assert "routes_covered" in data

    async def test_ml_clear(self, client, seed_data):
        resp = await client.delete("/api/ml/predict")
        assert resp.status_code == 200
        # После очистки прогнозов 0
        stats = await client.get("/api/ml/predict/stats")
        assert stats.json()["total_forecasts"] == 0


class TestPredict:
    """POST /api/predict — живой гибридный эндпоинт прогнозирования."""

    REQUIRED_FIELDS = {
        "route_id", "timestamp", "hour", "passengers_predicted",
        "passengers_lower", "passengers_upper",
        "is_weekend", "is_holiday", "weather_factor", "event_factor",
    }

    @pytest.fixture(autouse=True)
    def _force_statistical_mode(self, monkeypatch):
        """PREDICT_DISABLE_MODEL=1: тесты детерминированы и быстры, даже если
        артефакты CatBoost лежат в репозитории. Сбрасываем кэш загрузки,
        чтобы переопределение гарантированно применилось."""
        monkeypatch.setenv("PREDICT_DISABLE_MODEL", "1")
        model_worker.reset_for_test()

    async def test_predict_hourly_no_persist(self, client, seed_data):
        """persist=false: 200, 24 почасовых строк, все поля Forecast на месте."""
        resp = await client.post("/api/predict", json={
            "route_ids": [1],
            "horizon": "hour",
            "periods": 24,
            "persist": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "statistical"
        assert data["predicted"] == 24
        assert data["stored"] == 0
        rows = data["rows"]
        assert len(rows) == 24
        for r in rows:
            assert self.REQUIRED_FIELDS.issubset(r.keys())
            assert r["route_id"] == 1
            assert r["passengers_predicted"] >= 0
            assert r["passengers_lower"] == round(r["passengers_predicted"] * 0.8)
            assert r["passengers_upper"] == round(r["passengers_predicted"] * 1.2)
            assert r["passengers_lower"] <= r["passengers_predicted"] <= r["passengers_upper"]
        # Все часы заполнены (по одному на каждый час подряд)
        hours = [r["hour"] for r in rows]
        assert len(set(hours)) == 24

    async def test_predict_day_horizon(self, client, seed_data):
        """horizon=day periods=2: два дня с шагом сутки, все 24 часа в каждом дне."""
        resp = await client.post("/api/predict", json={
            "route_ids": [1],
            "start": "2026-10-01T00:00:00",
            "horizon": "day",
            "periods": 2,
            "persist": False,
        })
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert len(rows) == 48  # 2 дня × 24 часа
        days = sorted({r["timestamp"][:10] for r in rows})
        assert days == ["2026-10-01", "2026-10-02"]
        hours = sorted({r["hour"] for r in rows})
        assert hours == list(range(24))

    async def test_predict_invalid_route(self, client, seed_data):
        """Несуществующий маршрут → 400."""
        resp = await client.post("/api/predict", json={
            "route_ids": [999],
            "horizon": "hour",
            "periods": 3,
            "persist": False,
        })
        assert resp.status_code == 400

    async def test_predict_invalid_horizon(self, client, seed_data):
        """Неверный горизонт → 422 (Pydantic Literal)."""
        resp = await client.post("/api/predict", json={
            "route_ids": [1],
            "horizon": "minute",
            "periods": 3,
            "persist": False,
        })
        assert resp.status_code == 422

    async def test_predict_persist_and_readable(self, client, seed_data):
        """persist=true: строки читаются через GET /api/forecast,
        повторный вызов не создаёт дубликатов (UPSERT по route_id+timestamp)."""
        payload = {
            "route_ids": [1],
            "start": "2026-10-01T00:00:00",
            "horizon": "hour",
            "periods": 4,
            "persist": True,
        }
        resp = await client.post("/api/predict", json=payload)
        assert resp.status_code == 200
        assert resp.json()["stored"] == 4

        get = await client.get("/api/forecast", params={
            "route": 1, "from_date": "2026-10-01", "to_date": "2026-10-01",
        })
        assert get.status_code == 200
        assert len(get.json()) == 4

        # Повторный вызов: та же дата и часы — дубликатов нет
        again = await client.post("/api/predict", json=payload)
        assert again.json()["stored"] == 4
        get2 = await client.get("/api/forecast", params={
            "route": 1, "from_date": "2026-10-01", "to_date": "2026-10-01",
        })
        assert len(get2.json()) == 4

    async def test_predict_all_routes_default(self, client, seed_data):
        """Без route_ids — прогноз для всех маршрутов (3 в тестовой БД)."""
        resp = await client.post("/api/predict", json={
            "horizon": "hour",
            "periods": 3,
            "persist": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["predicted"] == 9  # 3 маршрута × 3 часа
        route_ids = {r["route_id"] for r in data["rows"]}
        assert route_ids == {1, 2, 3}

    async def test_predict_statistical_mode_reported(self, client, seed_data):
        """Без артефакта модели ответ сообщает mode='statistical'."""
        resp = await client.post("/api/predict", json={
            "route_ids": [1],
            "horizon": "hour",
            "periods": 1,
            "persist": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "statistical"
        assert data["model_path"] is None
        assert data["rows"][0]["passengers_predicted"] >= 0


def _catboost_available() -> bool:
    """Артефакты CatBoost есть в репозитории и библиотеки импортируются?"""
    from pathlib import Path

    try:
        import catboost  # noqa: F401
        import joblib  # noqa: F401
        import pandas  # noqa: F401
        import numpy  # noqa: F401
        from sklearn.preprocessing import MinMaxScaler  # noqa: F401
    except Exception:
        return False
    models_dir = Path(__file__).resolve().parent.parent / "models" / "artifacts"
    return any(models_dir.glob("catboost_route_*.joblib"))


@pytest.mark.skipif(
    not _catboost_available(),
    reason="Реальная CatBoost-модель не собрана (нет артефактов или библиотек)",
)
class TestPredictCatboostReal:
    """Реальная CatBoost-модель: один /predict с mode='catboost'.

    Работает только когда артефакты backend/models/artifacts/catboost_route_*.joblib
    присутствуют И библиотеки (catboost, scikit-learn, joblib) импортируются;
    иначе тест пропускается.
    """

    async def test_predict_real_catboost_mode(self, client, seed_data, monkeypatch):
        """Полноценный прогон: маршрут 1 (модель есть), период внутри метео-покрытия."""
        monkeypatch.delenv("PREDICT_DISABLE_MODEL", raising=False)
        model_worker.reset_for_test()

        resp = await client.post("/api/predict", json={
            "route_ids": [1],
            "start": "2025-03-01T00:00:00",
            "horizon": "hour",
            "periods": 6,
            "persist": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "catboost"
        assert data["model_path"] is not None
        assert data["predicted"] == 6
        rows = data["rows"]
        assert len(rows) == 6
        for r in rows:
            assert r["route_id"] == 1
            assert r["passengers_predicted"] >= 0
            assert r["passengers_lower"] == round(r["passengers_predicted"] * 0.8)
            assert r["passengers_upper"] == round(r["passengers_predicted"] * 1.2)
        # Ночи (часы 1-3) обнуляются как и в predict_1.py
        hours = {r["hour"]: r["passengers_predicted"] for r in rows}
        assert all(v == 0 for h, v in hours.items() if h in (1, 2, 3))