from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime, timedelta
from typing import Optional

from app.database import get_db
from app.models import Forecast, Route, Scenario
from app.schemas import ForecastOut, ForecastQuery, ScenarioCreate, ScenarioOut

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


@router.get("", response_model=list[ForecastOut])
async def get_forecast(
    route: Optional[int] = Query(None),
    stop: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    granularity: str = Query("hour", pattern="^(hour|day|month)$"),
    db: AsyncSession = Depends(get_db),
):
    """Получить прогноз пассажиропотока."""
    query = select(Forecast)

    if route is not None:
        query = query.where(Forecast.route_id == route)
    if from_date:
        try:
            dt_from = datetime.strptime(from_date, "%Y-%m-%d")
            query = query.where(Forecast.timestamp >= dt_from)
        except ValueError:
            raise HTTPException(400, "Некорректный from_date, используй YYYY-MM-DD")
    if to_date:
        try:
            dt_to = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)
            query = query.where(Forecast.timestamp < dt_to)
        except ValueError:
            raise HTTPException(400, "Некорректный to_date, используй YYYY-MM-DD")

    query = query.order_by(Forecast.timestamp, Forecast.hour)
    result = await db.execute(query)
    records = result.scalars().all()

    # Обогащаем номером маршрута
    output = []
    for r in records:
        route_obj = await db.get(Route, r.route_id)
        output.append(ForecastOut(
            route_id=r.route_id,
            route_number=route_obj.number if route_obj else None,
            timestamp=r.timestamp,
            hour=r.hour,
            passengers_predicted=r.passengers_predicted,
            passengers_lower=r.passengers_lower,
            passengers_upper=r.passengers_upper,
        ))

    # Агрегация по granularity
    if granularity == "day":
        daily = {}
        for f in output:
            day = f.timestamp.date()
            key = (f.route_id, day)
            if key not in daily:
                daily[key] = {"passengers": 0, "lower": 0, "upper": 0}
            daily[key]["passengers"] += f.passengers_predicted
            if f.passengers_lower:
                daily[key]["lower"] += f.passengers_lower
            if f.passengers_upper:
                daily[key]["upper"] += f.passengers_upper
        output = [
            ForecastOut(
                route_id=rid, route_number=None, timestamp=datetime.combine(day, datetime.min.time()),
                hour=0, passengers_predicted=v["passengers"],
                passengers_lower=v["lower"] or None, passengers_upper=v["upper"] or None
            )
            for (rid, day), v in sorted(daily.items(), key=lambda x: x[0][1])
        ]
    elif granularity == "month":
        monthly = {}
        for f in output:
            month_key = f.timestamp.strftime("%Y-%m")
            key = (f.route_id, month_key)
            if key not in monthly:
                monthly[key] = {"passengers": 0, "lower": 0, "upper": 0}
            monthly[key]["passengers"] += f.passengers_predicted
            if f.passengers_lower:
                monthly[key]["lower"] += f.passengers_lower
            if f.passengers_upper:
                monthly[key]["upper"] += f.passengers_upper
        output = [
            ForecastOut(
                route_id=rid, route_number=None, timestamp=datetime.strptime(mk + "-01", "%Y-%m-%d"),
                hour=0, passengers_predicted=v["passengers"],
                passengers_lower=v["lower"] or None, passengers_upper=v["upper"] or None
            )
            for (rid, mk), v in sorted(monthly.items(), key=lambda x: x[0][1])
        ]

    return output


@router.post("/scenario", response_model=ScenarioOut)
async def create_scenario(scenario: ScenarioCreate, db: AsyncSession = Depends(get_db)):
    """Создать сценарий с корректирующими коэффициентами."""
    db_scenario = Scenario(**scenario.model_dump())
    db.add(db_scenario)
    await db.commit()
    await db.refresh(db_scenario)
    return db_scenario


@router.get("/scenarios", response_model=list[ScenarioOut])
async def get_scenarios(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Scenario).order_by(Scenario.created_at.desc()))
    return result.scalars().all()


@router.delete("/scenario/{scenario_id}")
async def delete_scenario(scenario_id: int, db: AsyncSession = Depends(get_db)):
    scenario = await db.get(Scenario, scenario_id)
    if not scenario:
        raise HTTPException(404, "Сценарий не найден")
    await db.delete(scenario)
    await db.commit()
    return {"ok": True}