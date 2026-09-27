from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Route
from app.schemas import RouteOut

router = APIRouter(prefix="/api/routes", tags=["routes"])


@router.get("", response_model=list[RouteOut])
async def get_routes(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Route).order_by(Route.number))
    return result.scalars().all()