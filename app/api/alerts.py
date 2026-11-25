import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api._deps import Session, require_token
from app.models.alert import Alert
from app.schemas.alert import AlertPage, AlertResponse

router = APIRouter(prefix="/api/alerts", tags=["alerts"], dependencies=[Depends(require_token)])


@router.get("")
async def list_alerts(
    session: Session,
    resolved: bool = False,
    target_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlertPage:
    """Open alerts, critical first and then newest first; `resolved=true` gives the resolved ones instead."""
    query = select(Alert).where(Alert.resolved_at.is_not(None) if resolved else Alert.resolved_at.is_(None))
    if target_id is not None:
        query = query.where(Alert.target_id == target_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    order = (Alert.severity != "critical", Alert.last_seen_at.desc(), Alert.id)
    rows = await session.scalars(query.order_by(*order).limit(limit).offset(offset))
    return AlertPage(items=[AlertResponse.model_validate(a) for a in rows], total=int(total or 0))
