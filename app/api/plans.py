import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api._deps import Session, require_token
from app.enums import PlanStatus
from app.errors import AppHTTPException
from app.models.plan import Plan
from app.schemas.plan import PlanPage, PlanResponse

router = APIRouter(prefix="/api/plans", tags=["plans"], dependencies=[Depends(require_token)])


@router.get("")
async def list_plans(
    session: Session,
    target_id: uuid.UUID | None = None,
    status: PlanStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PlanPage:
    """Plans, newest first. `status=open` gives every target's current drift."""
    query = select(Plan)
    if target_id is not None:
        query = query.where(Plan.target_id == target_id)
    if status is not None:
        query = query.where(Plan.status == status)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(Plan.created_at.desc(), Plan.id).limit(limit).offset(offset))
    return PlanPage(items=[PlanResponse.model_validate(p) for p in rows], total=int(total or 0))


@router.get("/{plan_id}")
async def get_plan(session: Session, plan_id: uuid.UUID) -> PlanResponse:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise AppHTTPException(404, "There's no plan with that ID.", error_code="plan_not_found")
    return PlanResponse.model_validate(plan)
