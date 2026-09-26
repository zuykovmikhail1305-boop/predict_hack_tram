from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class Route(Base):
    __tablename__ = "routes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    number = Column(Integer, unique=True, nullable=False, comment="Номер маршрута из ngpt_route")
    name = Column(String(255), nullable=True, comment="Название маршрута")

    stops = relationship("Stop", back_populates="route", cascade="all, delete-orphan")
    forecasts = relationship("Forecast", back_populates="route", cascade="all, delete-orphan")


class Stop(Base):
    __tablename__ = "stops"

    id = Column(Integer, primary_key=True, autoincrement=True)
    route_id = Column(Integer, ForeignKey("routes.id"), nullable=False)
    name = Column(String(255), nullable=False)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)
    order_num = Column(Integer, nullable=True, comment="Порядок остановки на маршруте")
    is_terminal = Column(Boolean, default=False)

    route = relationship("Route", back_populates="stops")


class Forecast(Base):
    __tablename__ = "forecasts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    route_id = Column(Integer, ForeignKey("routes.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False, comment="Дата и час прогноза")
    hour = Column(Integer, nullable=False, comment="Час дня 0-23")
    passengers_predicted = Column(Integer, nullable=False, comment="Прогноз пассажиров")
    passengers_lower = Column(Integer, nullable=True, comment="Нижняя граница (квантиль 0.1)")
    passengers_upper = Column(Integer, nullable=True, comment="Верхняя граница (квантиль 0.9)")
    is_weekend = Column(Boolean, default=False)
    is_holiday = Column(Boolean, default=False)
    weather_factor = Column(Float, default=1.0)
    event_factor = Column(Float, default=1.0)

    route = relationship("Route", back_populates="forecasts")


class Scenario(Base):
    __tablename__ = "scenarios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), default="Текущий сценарий")
    weather_factor = Column(Float, default=1.0, comment="Корректировка на погоду")
    event_factor = Column(Float, default=1.0, comment="Корректировка на события")
    season_factor = Column(Float, default=1.0, comment="Корректировка на сезон")
    custom_factor = Column(Float, default=1.0, comment="Произвольный коэффициент")
    created_at = Column(DateTime, default=datetime.utcnow)


class CorrectionLog(Base):
    """Лог применённых корректировок для отчёта"""
    __tablename__ = "correction_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    route_id = Column(Integer, ForeignKey("routes.id"), nullable=True)
    correction_type = Column(String(100), comment="Тип коррекции: weather/event/season")
    old_value = Column(Float)
    new_value = Column(Float)
    delta_percent = Column(Float)
    applied_at = Column(DateTime, default=datetime.utcnow)