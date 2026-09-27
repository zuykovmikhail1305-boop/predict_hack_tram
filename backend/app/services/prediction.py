"""Гибридный движок прогнозирования пассажиропотока (живой эндпоинт POST /api/predict).

Зафиксированная архитектура
---------------------------
    POST /api/predict -> модель живёт в ВЫДЕЛЕННОМ daemon-потоке
    ("model-worker", запускается в lifespan приложения и "живёт, ожидая
    запросы") -> прогнозы UPSERT'ятся в таблицу forecasts -> фронтенд читает
    через существующий GET /api/forecast. Схема БД не меняется.

Гибридный режим модели
----------------------
1) "artifact" — найден артефакт модели:
       * env MODEL_PATH, либо
       * models/*.joblib в корне репозитория или в backend/models.
   Загрузка через joblib.load (импорт joblib защищён try/except; новых
   зависимостей НЕТ). Вызывается duck-typed метод predict(X), возвращающий
   array-like прогнозов.

   ПРЕДПОЛАГАЕМЫЙ КОНТРАКТ ФИЧ АРТЕФАКТА (задокументированное допущение):
   X — двумерный массив, одна строка на (маршрут × час):
       [route_index, hour, day_of_week, month, is_weekend, is_holiday,
        is_working_saturday, weather_factor, event_factor]
   где route_index — индекс маршрута в отсортированном по id списке
   запрошенных маршрутов. Если predict бросает исключение или возвращает
   несовместимое число значений — безопасный фолбэк на статистический
   режим для всего запроса. Квантильные границы (lower/upper) применяются
   к точечным значениям артефакта так же, как в статистике.

2) "statistical" — артефакт отсутствует/не загрузился. Детерминированный
   статистический бейзлайн:
       база маршрута (среднее из существующих строк forecasts, иначе
       детерминированная схема 50..275 по индексу маршрута, в духе seed_data)
       × почасовой профиль (утренний/вечерний пик, ночь)
       × множитель выходного дня / праздника / рабочей субботы
       × weather_factor × event_factor.
   Квантили: passengers_lower = round(pred * 0.8),
             passengers_upper = round(pred * 1.2)  (семантика 0.1/0.9).

Загрузка модели — LAZY: объект загружается ВНУТРИ потока model-worker при
первом запросе (старт приложения не блокируется); повторные запросы
переиспользуют загруженный объект.

Внешние данные (app.external_data)
----------------------------------
- Погода: Open-Meteo. Для ПРОШЛЫХ дат — archive API (переиспользуем
  app.external_data.get_weather_async); для БУДУЩИХ — Open-Meteo forecast API.
  При любой ошибке/таймауте — нейтральный weather_factor=1.0.
- Календарь: isdayoff.ru (app.external_data.get_calendar_async); при ошибке —
  локальный детерминированный список праздников РФ и признак выходного дня
  по weekday().
- Каникулы и события: локальные статические справочники из app.external_data.
Все внешние вызовы кэшируются по дате (включая негативное кэширование ошибок),
чтобы не дёргать API на каждый час прогноза.
"""

from __future__ import annotations

import asyncio
import calendar
import os
import queue
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

# Дополнительных зависимостей НЕ добавляем: joblib импортируется опционально.
try:
    import joblib  # type: ignore
except ImportError:  # pragma: no cover — опциональная зависимость
    joblib = None

# numpy доступен транзитивно через pandas; импорт тоже опционален.
try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None

MAX_ROWS_PER_REQUEST = 100_000
WORKER_TIMEOUT_SECONDS = 180

# ─── Детерминированные профили и коэффициенты (в духе seed_data.py) ─────────
HOUR_PROFILE = {
    (7, 9): 1.7,      # утренний пик
    (17, 19): 1.7,    # вечерний пик
    (0, 5): 0.05,     # ночь
    (10, 16): 1.0,    # день
}
WEEKEND_MULTIPLIER = 0.6
HOLIDAY_MULTIPLIER = 0.5
WORKING_SATURDAY_MULTIPLIER = 0.85
SCHOOL_HOLIDAY_MULTIPLIER = 0.9
NEUTRAL_FACTOR = 1.0

# Локальный детерминированный список праздников РФ (фолбэк для isdayoff.ru).
STATIC_RUSSIAN_HOLIDAYS = {
    (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8),
    (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4),
}


# ─── Небольшие вспомогательные функции ──────────────────────────────────────

def _hour_multiplier(hour: int) -> float:
    """Почасовой профиль: пики 7-9 и 17-19, ночь ~0, остальное — средне."""
    for (lo, hi), mult in HOUR_PROFILE.items():
        if lo <= hour <= hi:
            return mult
    return 0.65  # час 6 и вечер 20-23


def _weather_factor_from(weather: Optional[dict]) -> float:
    """Детерминированный перевод погоды в фактор спроса (нейтрально = 1.0)."""
    if not weather:
        return NEUTRAL_FACTOR
    precip = weather.get("precipitation") or 0
    snowfall = weather.get("snowfall") or 0
    temp = weather.get("temperature")
    factor = NEUTRAL_FACTOR
    if precip > 2.0 or snowfall > 0.5:
        factor = 0.85
    elif precip > 0.2:
        factor = 0.92
    if temp is not None and temp < -15:
        factor = min(factor, 0.8)
    return round(factor, 3)


def _event_factor_for(events: list[dict], dt: datetime) -> float:
    """Коэффициент событий по статическому справочнику app.external_data."""
    day = dt.strftime("%Y-%m-%d")
    for ev in events:
        if ev.get("date") == day:
            etype = (ev.get("type") or "").lower()
            if "holiday" in etype:
                return 0.9
            return 1.05
    return NEUTRAL_FACTOR


def _school_holiday_factor(dt: datetime) -> float:
    """Школьные каникулы (статический справочник) снижают пассажиропоток."""
    try:
        from app.external_data import get_school_holidays
        for h in get_school_holidays(dt.year):
            start = datetime.strptime(h["start"], "%Y-%m-%d")
            end = datetime.strptime(h["end"], "%Y-%m-%d") + timedelta(days=1)
            if start <= dt < end:
                return SCHOOL_HOLIDAY_MULTIPLIER
    except Exception:
        pass
    return NEUTRAL_FACTOR


def _combined_event_factor(events: list[dict], dt: datetime) -> float:
    """Итоговый event_factor: события × школьные каникулы (детерминированно)."""
    return round(_event_factor_for(events, dt) * _school_holiday_factor(dt), 3)


async def _fetch_weather_async(dt: datetime, hour: int) -> Optional[dict]:
    """Погода для (дата, час).

    Прошлые даты — archive API (переиспользуем app.external_data).
    Будущие даты — Open-Meteo forecast API (если недоступен → None,
    что означает нейтральный weather_factor=1.0).
    """
    date_str = dt.strftime("%Y-%m-%d")
    today = datetime.now().date()
    if dt.date() < today:
        try:
            from app.external_data import get_weather_async
            res = await asyncio.wait_for(get_weather_async(date_str, hour=hour), timeout=4)
            if res.get("source") == "open-meteo" and "temperature" in res:
                return {
                    "temperature": res.get("temperature"),
                    "precipitation": res.get("precipitation"),
                    "snowfall": res.get("snowfall"),
                }
        except Exception:
            return None
        return None
    try:
        import httpx
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": 55.7558,
            "longitude": 37.6176,
            "start_date": date_str,
            "end_date": date_str,
            "timezone": "Europe/Moscow",
            "hourly": ["temperature_2m", "precipitation", "snowfall"],
        }
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        times = data.get("hourly", {}).get("time", []) or []
        temps = data.get("hourly", {}).get("temperature_2m", []) or []
        precips = data.get("hourly", {}).get("precipitation", []) or []
        snows = data.get("hourly", {}).get("snowfall", []) or []
        for i, t in enumerate(times):
            h = int(str(t).split("T")[1].split(":")[0])
            if h == hour:
                return {
                    "temperature": temps[i] if i < len(temps) else None,
                    "precipitation": precips[i] if i < len(precips) else None,
                    "snowfall": snows[i] if i < len(snows) else None,
                }
    except Exception:
        return None
    return None


async def _fetch_calendar_async(dt: datetime) -> Optional[dict]:
    """Производственный календарь isdayoff.ru (при ошибке → None → фолбэк)."""
    try:
        from app.external_data import get_calendar_async
        return await asyncio.wait_for(get_calendar_async(dt.strftime("%Y-%m-%d")), timeout=4)
    except Exception:
        return None


def build_timestamps(start: datetime, horizon: str, periods: int) -> list[datetime]:
    """Список почасовых меток для горизонта прогноза.

    - hour:  `periods` часов подряд от `start`;
    - day:   `periods` дней, все 24 часа каждого дня;
    - month: `periods` месяцев, все часы всех дней каждого месяца.
    """
    if horizon == "hour":
        return [start + timedelta(hours=i) for i in range(periods)]
    if horizon == "day":
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        out: list[datetime] = []
        for i in range(periods):
            d = day + timedelta(days=i)
            out.extend(d + timedelta(hours=h) for h in range(24))
        return out
    if horizon == "month":
        out = []
        cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(periods):
            _, ndays = calendar.monthrange(cur.year, cur.month)
            for day_offset in range(ndays):
                d = cur + timedelta(days=day_offset)
                out.extend(d + timedelta(hours=h) for h in range(24))
            cur = (cur + timedelta(days=32)).replace(day=1)
        return out
    raise ValueError(f"Неизвестный горизонт: {horizon}")


def count_requested_rows(routes_count: int, start: datetime, horizon: str, periods: int) -> int:
    """Число строк, которое будет сгенерировано (для лимита 100_000)."""
    return routes_count * len(build_timestamps(start, horizon, periods))


def discover_artifact() -> Optional[Path]:
    """Порядок поиска артефакта: env MODEL_PATH → models/*.joblib
    (корень репозитория и backend/models)."""
    env_path = os.environ.get("MODEL_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate
    backend_dir = Path(__file__).resolve().parent.parent.parent  # .../backend
    root_dir = backend_dir.parent                              # корень репозитория
    for base in (root_dir, backend_dir):
        models_dir = base / "models"
        if models_dir.is_dir():
            artifacts = sorted(models_dir.glob("*.joblib"))
            if artifacts:
                return artifacts[0]
    return None


# ─── Примитивы очереди заданий ──────────────────────────────────────────────

class _ResultHolder:
    """threading.Event-обёртка результата задания потока model-worker."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: Any = None
        self._error: Optional[BaseException] = None

    def set_result(self, value: Any) -> None:
        self._result = value
        self._event.set()

    def set_exception(self, exc: BaseException) -> None:
        self._error = exc
        self._event.set()

    def get(self, timeout: float) -> Any:
        if not self._event.wait(timeout):
            raise TimeoutError("model-worker не ответил в течение таймаута")
        if self._error is not None:
            raise self._error
        return self._result


class _Job:
    """Задание, выполняемое ВНУТРИ потока model-worker."""

    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict, holder: _ResultHolder) -> None:
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.holder = holder

    def run(self) -> None:
        try:
            self.holder.set_result(self.fn(*self.args, **self.kwargs))
        except Exception as exc:  # noqa: BLE001 — пробрасываем в вызывающий поток
            self.holder.set_exception(exc)


# ─── Движок ─────────────────────────────────────────────────────────────────

class ModelWorker:
    """Владелец модели: эксклюзивный доступ через выделенный daemon-поток.

    - start() — запускает поток "model-worker" (вызывается из lifespan);
    - stop()  — аккуратно останавливает поток (вызывается из lifespan);
    - predict() — асинхронная точка входа: CPU-bound работа выполняется через
      asyncio.to_thread, поэтому event loop никогда не блокируется;
    - модель загружается ЛЕНИВО внутри потока при первом запросе.
    """

    def __init__(self) -> None:
        self._jobs: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_requested = False

        self._model: Any = None
        self._model_path: Optional[str] = None
        self._load_error: Optional[str] = None
        self._load_attempted = False

        # Кэши внешних данных — доступ только из потока model-worker
        # (задания выполняются последовательно, гонок нет).
        self._weather_cache: dict[str, Optional[dict]] = {}
        self._calendar_cache: dict[str, Optional[dict]] = {}
        self._events_cache: Optional[list[dict]] = None

    # ─── Жизненный цикл потока ─────────────────────────────────────────────
    def start(self) -> None:
        """Запустить поток (идемпотентно). Вызывается из lifespan приложения."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested = False
            thread = threading.Thread(target=self._run, name="model-worker", daemon=True)
            thread.start()
            self._thread = thread

    def stop(self) -> None:
        """Остановить поток: sentinel в очередь + join (≤10 c)."""
        with self._lock:
            thread = self._thread
            self._thread = None
            if thread is None or not thread.is_alive():
                return
            self._stop_requested = True
        try:
            self._jobs.put_nowait(None)
        except Exception:
            pass
        thread.join(timeout=10)

    def _run(self) -> None:
        """Тело потока: «живёт и ждёт запросы» из очереди заданий."""
        while True:
            job = self._jobs.get()
            if job is None:
                break
            job.run()

    def _submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Отправить работу в поток модели и дождаться результата."""
        self.start()
        holder = _ResultHolder()
        self._jobs.put(_Job(fn, args, kwargs, holder))
        return holder.get(timeout=WORKER_TIMEOUT_SECONDS)

    # ─── Публичный async-интерфейс ─────────────────────────────────────────
    async def predict(
        self,
        routes: list[dict],
        start: datetime,
        horizon: str,
        periods: int,
        base_loads: dict[int, float],
    ) -> dict:
        """Асинхронный прогноз для списка маршрутов.

        routes:     [{"id": int, "number": int, "index": int}] — index = позиция
                    в отсортированном по id списке (для фич артефакта и базы).
        base_loads: {route_id: float} — средняя нагрузка из существующих
                    строк forecasts (None → детерминированная схема).
        Возвращает: {"mode": "artifact"|"statistical", "model_path": ...,
                     "rows": [дикты полей Forecast, см. _build_row]}.
        """
        return await asyncio.to_thread(
            self._predict_blocking, routes, start, horizon, periods, base_loads,
        )

    def _predict_blocking(
        self,
        routes: list[dict],
        start: datetime,
        horizon: str,
        periods: int,
        base_loads: dict[int, float],
    ) -> dict:
        return self._submit(self._predict_on_worker, routes, start, horizon, periods, base_loads)

    # ─── Работа внутри потока модели ───────────────────────────────────────
    def _predict_on_worker(
        self,
        routes: list[dict],
        start: datetime,
        horizon: str,
        periods: int,
        base_loads: dict[int, float],
    ) -> dict:
        timestamps = build_timestamps(start, horizon, periods)
        model = self._ensure_model()
        if model is not None:
            try:
                rows = self._predict_with_artifact(model, routes, timestamps)
                return {"mode": "artifact", "model_path": self._model_path, "rows": rows}
            except Exception as exc:  # noqa: BLE001 — артефакт не отработал → фолбэк
                self._load_error = f"артефакт не отработал ({exc}); использована статистика"
        rows = self._predict_statistical(routes, timestamps, base_loads)
        return {"mode": "statistical", "model_path": None, "rows": rows}

    def _ensure_model(self) -> Any:
        if not self._load_attempted:
            self._load_attempted = True
            self._model = self._load_model()
        return self._model

    def _load_model(self) -> Any:
        path = discover_artifact()
        if path is None:
            self._load_error = "артефакт модели не найден"
            return None
        if joblib is None:
            self._load_error = "joblib не установлен"
            return None
        try:
            model = joblib.load(path)
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"не удалось загрузить {path}: {exc}"
            return None
        self._model_path = str(path)
        return model

    # ─── Статистический бейзлайн ───────────────────────────────────────────
    def _predict_statistical(
        self,
        routes: list[dict],
        timestamps: list[datetime],
        base_loads: dict[int, float],
    ) -> list[dict]:
        events = self._events()
        rows: list[dict] = []
        for ts in timestamps:
            flags = self._day_flags(ts)
            weather_factor = _weather_factor_from(self._weather_for(ts, ts.hour))
            event_factor = _combined_event_factor(events, ts)
            for route in routes:
                base = base_loads.get(route["id"])
                if base is None:
                    # Детерминированная схема в духе seed_data (50..300 по индексу).
                    base = 50 + (route["index"] % 10) * 25
                mult = _hour_multiplier(ts.hour)
                if flags["is_weekend"]:
                    mult *= WEEKEND_MULTIPLIER
                if flags["is_holiday"]:
                    mult *= HOLIDAY_MULTIPLIER
                if flags["is_working_saturday"]:
                    mult *= WORKING_SATURDAY_MULTIPLIER
                mult *= weather_factor * event_factor
                pred = max(0, int(round(base * mult)))
                rows.append(self._build_row(route, ts, flags, weather_factor, event_factor, pred))
        return rows

    # ─── Артефакт-режим ────────────────────────────────────────────────────
    def _predict_with_artifact(
        self,
        model: Any,
        routes: list[dict],
        timestamps: list[datetime],
    ) -> list[dict]:
        """Строит матрицу фич (контракт — в docstring модуля), вызывает
        model.predict(X). Несовпадение длины/исключение → фолбэк наверху."""
        matrix: list[list[float]] = []
        meta: list[tuple[dict, datetime, dict, float, float]] = []
        events = self._events()
        for ts in timestamps:
            flags = self._day_flags(ts)
            weather_factor = _weather_factor_from(self._weather_for(ts, ts.hour))
            event_factor = _combined_event_factor(events, ts)
            for route in routes:
                matrix.append([
                    route["index"], ts.hour, ts.weekday(), ts.month,
                    int(flags["is_weekend"]), int(flags["is_holiday"]),
                    int(flags["is_working_saturday"]), weather_factor, event_factor,
                ])
                meta.append((route, ts, flags, weather_factor, event_factor))
        X = np.asarray(matrix, dtype=float) if np is not None else matrix
        y = model.predict(X)
        values = list(y)
        if values and hasattr(values[0], "__iter__") and not isinstance(values[0], (str, bytes)):
            # Двумерный вывод — берём первый столбец.
            values = [row[0] for row in values]
        if len(values) != len(meta):
            raise ValueError(f"артефакт вернул {len(values)} значений на {len(meta)} строк")
        rows = []
        for value, (route, ts, flags, weather_factor, event_factor) in zip(values, meta):
            pred = max(0, int(round(float(value))))
            rows.append(self._build_row(route, ts, flags, weather_factor, event_factor, pred))
        return rows

    @staticmethod
    def _build_row(
        route: dict,
        ts: datetime,
        flags: dict,
        weather_factor: float,
        event_factor: float,
        pred: int,
    ) -> dict:
        """Строка ровно с полями модели Forecast."""
        return {
            "route_id": route["id"],
            "timestamp": ts,
            "hour": ts.hour,
            "passengers_predicted": pred,
            "passengers_lower": max(0, round(pred * 0.8)),
            "passengers_upper": round(pred * 1.2),
            "is_weekend": flags["is_weekend"],
            "is_holiday": flags["is_holiday"],
            "weather_factor": weather_factor,
            "event_factor": event_factor,
        }

    # ─── Внешние данные (кэшируются по дате) ───────────────────────────────
    def _day_flags(self, dt: datetime) -> dict:
        cal = self._calendar_for(dt)
        if cal:
            return {
                "is_weekend": bool(cal.get("is_weekend", dt.weekday() >= 5)),
                "is_holiday": bool(cal.get("is_holiday", (dt.month, dt.day) in STATIC_RUSSIAN_HOLIDAYS)),
                "is_working_saturday": bool(cal.get("is_working_saturday", False)),
            }
        return {
            "is_weekend": dt.weekday() >= 5,
            "is_holiday": (dt.month, dt.day) in STATIC_RUSSIAN_HOLIDAYS,
            "is_working_saturday": False,
        }

    def _calendar_for(self, dt: datetime) -> Optional[dict]:
        key = dt.strftime("%Y-%m-%d")
        if key not in self._calendar_cache:
            try:
                self._calendar_cache[key] = asyncio.run(_fetch_calendar_async(dt))
            except Exception:  # noqa: BLE001
                self._calendar_cache[key] = None
        return self._calendar_cache.get(key)

    def _weather_for(self, dt: datetime, hour: int) -> Optional[dict]:
        key = dt.strftime("%Y-%m-%d")
        if key not in self._weather_cache:
            try:
                self._weather_cache[key] = asyncio.run(_fetch_weather_async(dt, hour))
            except Exception:  # noqa: BLE001
                self._weather_cache[key] = None
        return self._weather_cache.get(key)

    def _events(self) -> list[dict]:
        if self._events_cache is None:
            try:
                from app.external_data import get_special_events
                self._events_cache = list(get_special_events())
            except Exception:  # noqa: BLE001
                self._events_cache = []
        return self._events_cache


# Модульный синглтон: в lifespan делается worker.start()/stop(),
# в тестах (без lifespan) поток стартует лениво при первом вызове predict().
model_worker = ModelWorker()