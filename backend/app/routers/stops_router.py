from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Stop
from app.schemas import StopOut

router = APIRouter(prefix="/api/stops", tags=["stops"])


@router.get("", response_model=list[StopOut])
async def get_stops(
    route: int | None = Query(None, description="Номер маршрута"),
    db: AsyncSession = Depends(get_db),
):
    query = select(Stop)
    if route is not None:
        query = query.where(Stop.route_id == route)
    query = query.order_by(Stop.route_id, Stop.order_num)
    result = await db.execute(query)
    return result.scalars().all()