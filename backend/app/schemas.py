from pydantic import BaseModel, Field
from typing import Literal, Optional, List
from datetime import datetime


class RouteOut(BaseModel):
    id: int
    number: int
    name: Optional[str] = None

    class Config:
        from_attributes = True


class StopOut(BaseModel):
    id: int
    route_id: int
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    order_num: Optional[int] = None
    is_terminal: bool = False

    class Config:
        from_attributes = True


class ForecastOut(BaseModel):
    route_id: int
    route_number: Optional[int] = None
    timestamp: datetime
    hour: int
    passengers_predicted: int
    passengers_lower: Optional[int] = None
    passengers_upper: Optional[int] = None

    class Config:
        from_attributes = True


class HourPoint(BaseModel):
    """Точка почасового пассажиропотока (факт текущего часа или прогноз следующего)."""
    timestamp: datetime
    hour: int
    passengers: int
    passengers_lower: Optional[int] = None
    passengers_upper: Optional[int] = None


class NextHourForecastOut(BaseModel):
    """Ответ /api/forecast/next-hour: факт «текущего» часа + прогноз на следующий час."""
    route_id: Optional[int] = None
    route_number: Optional[int] = None
    current: Optional[HourPoint] = None
    next: Optional[HourPoint] = None
    diff_abs: Optional[float] = None
    diff_pct: Optional[float] = None
    is_fallback: bool = False
    note: Optional[str] = None


class ForecastQuery(BaseModel):
    route: Optional[int] = None
    stop: Optional[int] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    granularity: str = Field(default="hour", pattern="^(hour|day|month)$")


class PredictRequest(BaseModel):
    """Тело запроса POST /api/predict (живой гибридный прогноз)."""

    route_ids: Optional[List[int]] = Field(
        default=None,
        description="ID маршрутов; null — все маршруты",
    )
    start: Optional[datetime] = Field(
        default=None,
        description="Начало прогноза; null — текущий час сервера",
    )
    horizon: Literal["hour", "day", "month"] = Field(
        default="hour",
        description="Шаг прогноза: почасовой / подневной / помесячный",
    )
    periods: int = Field(
        default=24,
        ge=1,
        description="Количество периодов (часов/дней/месяцев); "
        "default: hour→24, day→7, month→3",
    )
    persist: bool = Field(
        default=True,
        description="Сохранить прогноз в таблицу forecasts (UPSERT)",
    )


class PredictForecastRow(BaseModel):
    """Строка прогноза — ровно поля модели Forecast (для ответа и вставки в БД)."""

    route_id: int
    timestamp: datetime
    hour: int
    passengers_predicted: int
    passengers_lower: Optional[int] = None
    passengers_upper: Optional[int] = None
    is_weekend: bool = False
    is_holiday: bool = False
    weather_factor: float = 1.0
    event_factor: float = 1.0


class PredictResponse(BaseModel):
    """Ответ POST /api/predict: режим модели, счётчики и строки прогноза."""

    mode: Literal["catboost", "statistical"]
    model_path: Optional[str] = None
    csv_path: Optional[str] = None
    predicted: int = 0
    stored: int = 0
    rows: List[PredictForecastRow] = Field(default_factory=list)


class ScenarioCreate(BaseModel):
    weather_factor: float = Field(default=1.0, ge=0.0, le=3.0)
    event_factor: float = Field(default=1.0, ge=0.0, le=3.0)
    season_factor: float = Field(default=1.0, ge=0.0, le=3.0)
    custom_factor: float = Field(default=1.0, ge=0.0, le=3.0)
    name: str = "Сценарий"


class ScenarioOut(BaseModel):
    id: int
    name: str
    weather_factor: float
    event_factor: float
    season_factor: float
    custom_factor: float
    created_at: datetime

    class Config:
        from_attributes = True