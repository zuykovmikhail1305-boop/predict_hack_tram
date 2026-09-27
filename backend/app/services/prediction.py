"""Гибридный движок прогнозирования пассажиропотока (живой эндпоинт POST /api/predict).

Зафиксированная архитектура
---------------------------
    POST /api/predict -> модель живёт в ВЫДЕЛЕННОМ daemon-потоке
    ("model-worker", запускается в lifespan приложения и «живёт, ожидая
    запросы») -> прогнозы UPSERT'ятся в таблицу forecasts -> фронтенд читает
    через существующий GET /api/forecast. Схема БД не меняется.

Гибридный режим модели
----------------------
1) "catboost" — реальные модели CatBoost (порт predict_1.py):
       * артефакты backend/models/artifacts/catboost_route_*.joblib
         (env MODEL_PATH может указывать на каталог с артефактами или файл);
       * конфиги backend/models/config/{sport_events,school_holidays,
         new_holiday,meteo_config}.json;
       * данные backend/models/data/{meteo.csv,labels_day_train.csv}.
   Признаковый пайплайн портирован из predict_1.py практически дословно
   (точные имена и порядок столбцов — CatBoost чувствителен к порядку,
   проверка через model.feature_names_ функцией check_feature_order).
   MinMaxScaler подгоняется ОДИН раз при загрузке модели из bundled
   labels_day_train.csv + meteo.csv ровно как в predict_1.py
   (csv_round_trip(build_features(...)) -> fit_scaler) — вся CPU-bound
   работа выполняется ВНУТРИ потока model-worker.

   Модель выдаёт ТОЧЕЧНЫЕ прогнозы; квантильные границы lower/upper —
   ЭВРИСТИКА: lower=round(p*0.8), upper=round(p*1.2) (документированное
   допущение в режиме catboost).

   Погода: bundled meteo.csv покрывает период датасета (2025 год). Для дат
   вне него merge даёт NaN по погоде — CatBoost умеет работать с пропусками
   нативно (документировано; backfill через Open-Meteo ВНЕ объёма задачи).

   ЛЮБАЯ ошибка при загрузке/прогнозе (нет файлов, неудачный fit, ошибки
   импорта, несовпадение порядка фич) -> лог-warning и безопасный фолбэк
   на статистический режим для всего запроса. Запрос никогда не падает.

2) "statistical" — артефакт отсутствует/не загрузился/отключён env
   PREDICT_DISABLE_MODEL. Детерминированный статистический бейзлайн:
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

Переопределения окружения
-------------------------
- PREDICT_DISABLE_MODEL=1 — принудительно статистический режим (тесты).
- MODEL_PATH=<каталог|файл> — путь к артефактам модели вместо
  backend/models/artifacts (каталог) или родитель файла.

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
import io
import json
import logging
import os
import queue
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Тяжёлые/опциональные зависимости импортируются защищённо (try/except),
# чтобы приложение стартовало и обслуживало статистический режим без них.
try:
    import joblib  # type: ignore
except ImportError:  # pragma: no cover — опциональная зависимость
    joblib = None

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None

try:
    import pandas as pd  # type: ignore
except ImportError:  # pragma: no cover
    pd = None

try:
    from sklearn.preprocessing import MinMaxScaler  # type: ignore
except ImportError:  # pragma: no cover — опциональная зависимость
    MinMaxScaler = None

MAX_ROWS_PER_REQUEST = 100_000
WORKER_TIMEOUT_SECONDS = 180

# ─── Константы пайплайна predict_1.py ────────────────────────────────────────

# столбцы, которые масштабируются MinMaxScaler (как nums_col в model.ipynb)
NUMS_COL = [
    "temperature_2m (°C)", "apparent_temperature (°C)",
    "precipitation (mm)", "rain (mm)", "snowfall_mm",
    "snow_depth_mm", "pressure_msl (hPa)",
    "wind_speed_10m (km/h)", "wind_gusts_10m (km/h)",
]

# маршрута без собственной модели — используется модель этого маршрута
FALLBACK_ROUTE = 1

# с 01:00 до 04:00 транспорт не ходит - прогноз в эти часы всегда 0
NO_SERVICE_HOURS = [1, 2, 3]

# env-переключатель принудительного статистического режима (тесты)
ENV_DISABLE_MODEL = "PREDICT_DISABLE_MODEL"

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


# ─── Поиск файлов модели ─────────────────────────────────────────────────────

def _backend_dir() -> Path:
    """Каталог backend (родитель пакета app)."""
    return Path(__file__).resolve().parent.parent.parent


def models_root() -> Path:
    """backend/models — корень данных модели, относительно пакета backend."""
    return _backend_dir() / "models"


def discover_models_dir() -> Optional[Path]:
    """Каталог с артефактами catboost_route_*.joblib.

    Порядок: env MODEL_PATH (каталог или родитель файла) →
    backend/models/artifacts → <корень репозитория>/models/artifacts.
    """
    env_path = os.environ.get("MODEL_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_dir():
            return candidate
        if candidate.is_file():
            return candidate.parent
    backend = _backend_dir()
    for base in (backend, backend.parent):
        artifacts_dir = base / "models" / "artifacts"
        if artifacts_dir.is_dir():
            return artifacts_dir
    return None


# ─── Признаковый пайплайн (порт из predict_1.py) ────────────────────────────

def load_sport_events(path):
    """Прочитать расписание матчей: {вид спорта: DataFrame[date, hour, team]}."""
    with open(path, encoding="utf-8") as file:
        events = json.load(file)

    schedules = {}
    for sport in ("football", "hockey"):
        matches = pd.DataFrame(events[sport]["matches"], columns=["date", "hour", "team"])
        matches["date"] = pd.to_datetime(matches["date"], format="%Y-%m-%d")
        matches["hour"] = matches["hour"].astype(int)
        matches["team"] = matches["team"] + events[sport]["column_suffix"]
        schedules[sport] = matches.drop_duplicates()
    return schedules


def add_sport_features(data, matches):
    """Флаг дня матча и пикового окна (от -2 до +3 часов от начала) для каждого клуба."""
    result = data.copy()
    record_datetime = result["date"] + pd.to_timedelta(result["hour"], unit="h")

    for team in matches["team"].unique():
        result[team] = 0
        result[f"{team}_peak"] = 0

    for match_date, match_hour, team in matches.itertuples(index=False):
        match_datetime = match_date + pd.Timedelta(hours=match_hour)
        peak_period = record_datetime.between(
            match_datetime - pd.Timedelta(hours=2),
            match_datetime + pd.Timedelta(hours=3),
        )
        result.loc[result["date"].eq(match_date), team] = 1
        result.loc[peak_period, f"{team}_peak"] = 1

    return result


def prepare_meteo(raw, config_path):
    """Почасовая погода: ключи date/hour, снег в мм, ветер через sin/cos, код погоды по группам."""
    with open(config_path, encoding="utf-8") as file:
        config = json.load(file)
    drop_columns = config["drop_columns"]
    weather_code_groups = config["weather_code_groups"]

    missing = set(drop_columns) - set(raw.columns)
    if missing:
        raise KeyError(f"В meteo нет столбцов: {sorted(missing)}")

    meteo = raw.drop(columns=drop_columns)
    timestamp = pd.to_datetime(meteo.pop("time"), format="%Y-%m-%dT%H:%M")
    meteo.insert(0, "date", timestamp.dt.normalize())
    meteo.insert(1, "hour", timestamp.dt.hour.astype(int))

    meteo["snowfall_mm"] = meteo.pop("snowfall (cm)") * 10
    meteo["snow_depth_mm"] = meteo.pop("snow_depth (m)") * 1000

    wind_direction = np.radians(meteo.pop("wind_direction_10m (°)"))
    meteo["wind_direction_sin"] = np.sin(wind_direction)
    meteo["wind_direction_cos"] = np.cos(wind_direction)

    weather_code = meteo.pop("weather_code (wmo code)")
    known_codes = {code for codes in weather_code_groups.values() for code in codes}
    unknown_codes = set(weather_code.unique()) - known_codes
    if unknown_codes:
        raise ValueError(f"Неизвестные коды погоды: {sorted(unknown_codes)}")
    for column, codes in weather_code_groups.items():
        meteo[column] = weather_code.isin(codes).astype("int8")

    return meteo


def merge_weather_data(main_df, weather_df):
    merge_keys = ["date", "hour"]
    weather = weather_df.drop_duplicates(subset=merge_keys)

    overlapping_columns = set(main_df.columns).intersection(weather.columns) - set(merge_keys)
    weather = weather.drop(columns=list(overlapping_columns))

    result = main_df.merge(weather, on=merge_keys, how="left", validate="many_to_one")

    weather_columns = weather.columns.difference(merge_keys)
    missing_rows = result[weather_columns].isna().any(axis=1).sum()
    if missing_rows:
        logger.info("Прогноз: %s строк без погоды (NaN уйдёт в CatBoost)", missing_rows)
    return result


def add_calendar_features(data, calendar_path):
    """Признаки производственного календаря.

    day_off         - любой нерабочий день, включая обычные выходные
    public_holiday  - нерабочий будний день (праздник или перенос)
    working_weekend - рабочая суббота или воскресенье (перенос)
    short_day       - сокращённый предпраздничный день
    """
    with open(calendar_path, encoding="utf-8") as file:
        calendar_json = json.load(file)

    non_working_days = pd.to_datetime(calendar_json["nonWorkingDays"])
    short_days = pd.to_datetime(calendar_json["shortDays"])

    result = data.copy()
    is_weekend = result["date"].dt.dayofweek >= 5
    day_off = result["date"].isin(non_working_days)

    result["day_off"] = day_off.astype("int8")
    result["public_holiday"] = (day_off & ~is_weekend).astype("int8")
    result["working_weekend"] = (~day_off & is_weekend).astype("int8")
    result["short_day"] = result["date"].isin(short_days).astype("int8")
    return result


def add_school_holiday_features(data, holidays_path):
    with open(holidays_path, encoding="utf-8") as file:
        periods = json.load(file)["periods"]

    result = data.copy()
    in_holiday = pd.Series(False, index=result.index)
    for period in periods:
        in_holiday |= result["date"].between(
            pd.Timestamp(period["start"]), pd.Timestamp(period["end"])
        )

    result["school_holiday"] = in_holiday.astype("int8")
    return result


def add_time_features(data):
    """Циклическое кодирование часа, дня недели, месяца и дня года."""
    result = data.copy()
    dates = result["date"]
    cycles = {
        "hour": (result["hour"], 24),
        "dayofweek": (dates.dt.dayofweek, 7),
        "month": (dates.dt.month - 1, 12),
        "dayofyear": (dates.dt.dayofyear - 1, 365),
    }
    for name, (values, period) in cycles.items():
        angle = 2 * np.pi * values / period
        result[f"{name}_sin"] = np.sin(angle)
        result[f"{name}_cos"] = np.cos(angle)

    result["dayofweek"] = dates.dt.dayofweek
    result["day"] = dates.dt.day
    return result


def apply_feature_pipeline(data, sport_schedules, meteo, calendar_path, holidays_path):
    """Цепочка признаков из predict_1.build_features для уже загруженного DataFrame.

    Порядок применения и, соответственно, порядок столбцов совпадает с
    predict_1.py/build_features (порядок важен: CatBoost берёт признаки по
    порядку, сверка — check_feature_order).
    """
    for matches in sport_schedules.values():
        data = add_sport_features(data, matches)
    data = merge_weather_data(data, meteo)
    data = add_calendar_features(data, calendar_path)
    data = add_school_holiday_features(data, holidays_path)
    data = add_time_features(data)
    return data


def build_features(labels_path, sport_schedules, meteo, calendar_path, holidays_path):
    """Читает labels (route;date;hour;boardings) и строит полный набор признаков."""
    data = pd.read_csv(labels_path, sep=";")
    data["date"] = pd.to_datetime(data["date"], format="%Y-%m-%d")
    data["hour"] = data["hour"].astype(int)
    return apply_feature_pipeline(data, sport_schedules, meteo, calendar_path, holidays_path)


def csv_round_trip(data):
    """Записать и прочитать таблицу так же, как labels.ipynb сохраняет её, а model.ipynb читает.

    После CSV дробные значения отличаются в последнем знаке, и модели обучены
    именно на прочитанных из CSV значениях.
    """
    buffer = io.StringIO()
    data.to_csv(buffer, sep=";", index=False)
    buffer.seek(0)
    return pd.read_csv(buffer, sep=";")


def fit_scaler(train_features):
    """MinMaxScaler по train, как в ячейке с nums_col в model.ipynb."""
    scaler = MinMaxScaler()
    scaler.fit(train_features[NUMS_COL])
    return scaler


def scale_features(features, scaler):
    """Матрица признаков для модели: без date/route/целевого столбца, NUMS_COL через MinMaxScaler."""
    X = features.drop(columns=["date", "route", "boardings", "prediction"], errors="ignore").copy()
    X[NUMS_COL] = scaler.transform(X[NUMS_COL])
    return X


def check_feature_order(X, model):
    # CatBoost берёт признаки по порядку, поэтому порядок должен совпадать с обучением
    expected = list(model.feature_names_)
    actual = list(X.columns)
    if actual != expected:
        raise ValueError(f"Признаки не совпадают с обучением модели:\n{actual}\n{expected}")


class CatBoostEnsemble:
    """Загруженный ансамбль per-route CatBoost-моделей + scaler + пайплайн.

    Объект создаётся ОДИН раз внутри потока model-worker (CPU-bound load:
    fit MinMaxScaler на bundled train features, чтение joblib-артефактов).
    """

    def __init__(self, models, scaler, sport_schedules, meteo,
                 calendar_path, holidays_path, artifacts_dir):
        self.models = models  # {номер маршрута: CatBoostRegressor}
        self.scaler = scaler
        self.sport_schedules = sport_schedules
        self.meteo = meteo
        self.calendar_path = calendar_path
        self.holidays_path = holidays_path
        self.artifacts_dir = artifacts_dir

    def predict(self, features):
        """Точечный прогноз по строкам features (порт predict_1.predict)."""
        X = scale_features(features, self.scaler)
        prediction = pd.Series(np.nan, index=features.index)

        for route in sorted(features["route"].unique()):
            model_route = route if route in self.models else FALLBACK_ROUTE
            if model_route != route:
                logger.info("Маршрут %s: своей модели нет, используется модель маршрута %s",
                            route, model_route)
            model = self.models[model_route]
            check_feature_order(X, model)

            mask = features["route"].eq(route)
            prediction[mask] = model.predict(X[mask])

        prediction[features["hour"].isin(NO_SERVICE_HOURS)] = 0
        # CatBoost с MAE может дать число меньше нуля — prediction >= 0
        return prediction.clip(lower=0)


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

        self._model: Any = None          # CatBoostEnsemble | None
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
                    в отсортированном по id списке (для статистической базы).
        base_loads: {route_id: float} — средняя нагрузка из существующих
                    строк forecasts (None → детерминированная схема).
        Возвращает: {"mode": "catboost"|"statistical", "model_path": ...,
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
                rows = self._predict_with_catboost(model, routes, timestamps)
                return {"mode": "catboost", "model_path": self._model_path, "rows": rows}
            except Exception as exc:  # noqa: BLE001 — модель не отработала → фолбэк
                self._load_error = f"модель CatBoost не отработала ({exc}); использована статистика"
                logger.warning("CatBoost не отработал на запросе, фолбэк на статистику: %s", exc)
        rows = self._predict_statistical(routes, timestamps, base_loads)
        return {"mode": "statistical", "model_path": None, "rows": rows}

    def _ensure_model(self) -> Any:
        if not self._load_attempted:
            self._load_attempted = True
            self._model = self._load_model()
        return self._model

    def _load_model(self) -> Any:
        """Загрузка CatBoost-ансамбля ВНУТРИ потока worker (лениво, один раз).

        Любая проблема → лог-warning + None → статистический режим.
        """
        if os.environ.get(ENV_DISABLE_MODEL):
            self._load_error = f"модель отключена env {ENV_DISABLE_MODEL}=1"
            return None
        if pd is None or np is None or joblib is None or MinMaxScaler is None:
            self._load_error = "не установлены pandas/numpy/joblib/scikit-learn"
            logger.warning("Модель CatBoost недоступна: %s", self._load_error)
            return None
        try:
            import catboost  # noqa: F401 — проверка доступности библиотеки
        except ImportError:
            self._load_error = "catboost не установлен"
            logger.warning("Модель CatBoost недоступна: %s", self._load_error)
            return None

        artifacts_dir = discover_models_dir()
        if artifacts_dir is None:
            self._load_error = "артефакты модели не найдены (backend/models/artifacts)"
            logger.warning("Модель CatBoost недоступна: %s", self._load_error)
            return None
        try:
            bundle = self._build_ensemble(artifacts_dir)
        except Exception as exc:  # noqa: BLE001 — невалидные/битые файлы → статистика
            self._load_error = f"не удалось загрузить модель CatBoost: {exc}"
            logger.warning("Модель CatBoost недоступна: %s", self._load_error)
            return None
        self._model_path = str(artifacts_dir)
        logger.info("CatBoost загружен: %s моделей из %s",
                    len(bundle.models), artifacts_dir)
        return bundle

    def _build_ensemble(self, artifacts_dir: Path) -> CatBoostEnsemble:
        """Собрать ансамбль: конфиги, погода, scaler (fit), per-route модели."""
        config_dir = models_root() / "config"
        data_dir = models_root() / "data"

        sport_path = config_dir / "sport_events.json"
        calendar_path = config_dir / "new_holiday.json"
        holidays_path = config_dir / "school_holidays.json"
        meteo_config_path = config_dir / "meteo_config.json"
        labels_path = data_dir / "labels_day_train.csv"
        meteo_path = data_dir / "meteo.csv"
        for path in (sport_path, calendar_path, holidays_path, meteo_config_path,
                     labels_path, meteo_path):
            if not path.is_file():
                raise FileNotFoundError(f"нет файла модели: {path}")

        sport_schedules = load_sport_events(sport_path)
        meteo = prepare_meteo(pd.read_csv(meteo_path), meteo_config_path)

        # fit MinMaxScaler ОДИН раз — как в predict_1.main()
        train_features = csv_round_trip(
            build_features(labels_path, sport_schedules, meteo, calendar_path, holidays_path)
        )
        scaler = fit_scaler(train_features)

        models = {}
        for artifact in sorted(artifacts_dir.glob("catboost_route_*.joblib")):
            route_number = int(artifact.stem.rsplit("_", 1)[-1])
            models[route_number] = joblib.load(artifact)
        if not models:
            raise FileNotFoundError(f"нет моделей catboost_route_*.joblib в {artifacts_dir}")

        return CatBoostEnsemble(
            models=models,
            scaler=scaler,
            sport_schedules=sport_schedules,
            meteo=meteo,
            calendar_path=calendar_path,
            holidays_path=holidays_path,
            artifacts_dir=artifacts_dir,
        )

    def reset_for_test(self) -> None:
        """Сброс кэша загрузки модели (используется тестами)."""
        with self._lock:
            self._load_attempted = False
            self._model = None
            self._model_path = None
            self._load_error = None

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

    # ─── Режим CatBoost ────────────────────────────────────────────────────
    def _predict_with_catboost(
        self,
        bundle: CatBoostEnsemble,
        routes: list[dict],
        timestamps: list[datetime],
    ) -> list[dict]:
        """Строит датафрейм (маршрут × час), прогоняет пайплайн predict_1.py
        и возвращает строки ответа. Порядок строк: время → маршрут."""
        features = csv_round_trip(apply_feature_pipeline(
            self._request_frame(routes, timestamps),
            bundle.sport_schedules,
            bundle.meteo,
            bundle.calendar_path,
            bundle.holidays_path,
        ))
        prediction = bundle.predict(features)
        if prediction.isna().any():
            raise ValueError("не для всех строк получен прогноз")

        events = self._events()
        values = prediction.round().astype(int).tolist()
        rows: list[dict] = []
        idx = 0
        for ts in timestamps:
            flags = self._day_flags(ts)
            weather_factor = _weather_factor_from(self._weather_for(ts, ts.hour))
            event_factor = _combined_event_factor(events, ts)
            for route in routes:
                pred = max(0, int(values[idx]))
                idx += 1
                rows.append(self._build_row(route, ts, flags, weather_factor, event_factor, pred))
        return rows

    @staticmethod
    def _request_frame(routes: list[dict], timestamps: list[datetime]):
        """Датафрейм для пайплайна: одна строка на (маршрут × час).

        Колонки как в labels: route (номер маршрута), date (полночь), hour.
        boardings — фиктивная (scale_features её отбрасывает).
        """
        records = []
        for ts in timestamps:
            for route in routes:
                records.append({
                    "route": int(route["number"]),
                    "date": ts.date(),
                    "hour": int(ts.hour),
                })
        frame = pd.DataFrame(records)
        frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d")
        frame["hour"] = frame["hour"].astype(int)
        frame["boardings"] = 0
        return frame

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