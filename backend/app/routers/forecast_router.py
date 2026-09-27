from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime, timedelta
from typing import Optional

from app.database import get_db
from app.models import Forecast, Route, Scenario
from app.schemas import (
    ForecastOut, ForecastQuery, ScenarioCreate, ScenarioOut,
    HourPoint, NextHourForecastOut,
)

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


@router.get("/next-hour", response_model=NextHourForecastOut)
async def get_next_hour_forecast(
    route_id: Optional[int] = Query(None, description="ID маршрута; без параметра — сумма по всем маршрутам"),
    db: AsyncSession = Depends(get_db),
):
    """
    Факт «текущего» часа + прогноз на следующий час для KPI-карточки.

    В БД хранятся только почасовые ПРОГНОЗЫ (таблица forecasts) —
    фактических замеров пассажиропотока нет. Поэтому:
      * за «факт текущего часа» берётся запись ровно за текущий час сервера,
        а если её нет — последний доступный час в данных (is_fallback=true);
      * за «прогноз на следующий час» — следующая почасовая запись;
      * если следующего часа в данных ещё нет (конец диапазона), берётся
        пара «предпоследний → последний» час (is_fallback=true).
    При полном отсутствии данных возвращается пустой ответ без ошибки 500.
    """
    query = select(Forecast).order_by(Forecast.timestamp)
    if route_id is not None:
        query = query.where(Forecast.route_id == route_id)
    result = await db.execute(query)
    records = result.scalars().all()

    route_number = None
    if route_id is not None:
        route_obj = await db.get(Route, route_id)
        route_number = route_obj.number if route_obj else None

    if not records:
        return NextHourForecastOut(
            route_id=route_id,
            route_number=route_number,
            note="Нет данных для прогноза на следующий час",
        )

    # Собираем почасовые агрегаты: ключ — timestamp, значение — (пассажиры, нижняя, верхняя, час).
    # Для конкретного маршрута — это его записи; без route_id — сумма по всем маршрутам.
    by_ts = {}
    for rec in records:
        prev = by_ts.get(rec.timestamp)
        if prev is None:
            by_ts[rec.timestamp] = (
                rec.passengers_predicted, rec.passengers_lower, rec.passengers_upper, rec.hour,
            )
        else:
            p, lo, hi, hour = prev
            by_ts[rec.timestamp] = (
                p + rec.passengers_predicted,
                (lo or 0) + (rec.passengers_lower or 0),
                (hi or 0) + (rec.passengers_upper or 0),
                hour,
            )

    ordered_ts = sorted(by_ts.keys())

    def make_point(ts: datetime) -> HourPoint:
        p, lo, hi, hour = by_ts[ts]
        return HourPoint(
            timestamp=ts,
            hour=hour,
            passengers=p,
            passengers_lower=lo,
            passengers_upper=hi,
        )

    # Референс «текущего» часа: точное совпадение с текущим часом сервера.
    now_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    current_ts = now_hour if now_hour in by_ts else None
    is_fallback = current_ts is None

    if current_ts is None:
        # Данных ровно за текущий час нет — используем последний доступный час.
        current_ts = ordered_ts[-1]

    next_ts = current_ts + timedelta(hours=1)
    current_point = make_point(current_ts)
    next_point = make_point(next_ts) if next_ts in by_ts else None

    # Следующего часа нет (конец диапазона данных): показываем последний
    # известный прирост — пара «предпоследний → последний» час.
    if next_point is None and len(ordered_ts) >= 2:
        current_ts = ordered_ts[-2]
        next_ts = ordered_ts[-1]
        current_point = make_point(current_ts)
        next_point = make_point(next_ts)
        is_fallback = True

    diff_abs = None
    diff_pct = None
    if next_point is not None:
        diff_abs = float(next_point.passengers - current_point.passengers)
        diff_pct = round(diff_abs / current_point.passengers * 100, 1) if current_point.passengers else None

    note = None
    if is_fallback:
        note = (
            f"Записей ровно за текущий час ({now_hour:%Y-%m-%d %H:%M}) в БД нет; "
            f"показан последний доступный час: {current_ts:%Y-%m-%d %H:%M}."
        )
    elif next_point is None:
        note = "Следующего часа в данных пока нет — сравнение не выполнено."

    return NextHourForecastOut(
        route_id=route_id,
        route_number=route_number,
        current=current_point,
        next=next_point,
        diff_abs=diff_abs,
        diff_pct=diff_pct,
        is_fallback=is_fallback,
        note=note,
    )


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