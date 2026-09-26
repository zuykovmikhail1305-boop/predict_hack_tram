from pydantic import BaseModel, Field
from typing import Optional
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


class ForecastQuery(BaseModel):
    route: Optional[int] = None
    stop: Optional[int] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    granularity: str = Field(default="hour", pattern="^(hour|day|month)$")


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