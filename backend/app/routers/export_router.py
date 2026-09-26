import csv, io
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime

from app.database import get_db
from app.models import Forecast, Route

router = APIRouter(prefix="/api/forecast/export", tags=["export"])


@router.get("")
async def export_forecast(
    route: int | None = Query(None),
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    db: AsyncSession = Depends(get_db),
):
    query = select(Forecast)
    if route is not None:
        query = query.where(Forecast.route_id == route)
    query = query.order_by(Forecast.timestamp, Forecast.hour)
    result = await db.execute(query)
    records = result.scalars().all()

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["route_id", "date", "hour", "passengers_predicted", "passengers_lower", "passengers_upper"])
        for r in records:
            writer.writerow([r.route_id, r.timestamp.date(), r.hour, r.passengers_predicted, r.passengers_lower or "", r.passengers_upper or ""])
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=forecast.csv"},
        )

    # XLSX
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Forecast"
    ws.append(["route_id", "date", "hour", "passengers_predicted", "passengers_lower", "passengers_upper"])
    for r in records:
        ws.append([r.route_id, str(r.timestamp.date()), r.hour, r.passengers_predicted, r.passengers_lower or "", r.passengers_upper or ""])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=forecast.xlsx"},
    )