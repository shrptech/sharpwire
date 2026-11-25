from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api._deps import Session, require_token
from app.models.plan import Plan
from app.models.target import Target
from app.schemas.target import TargetResponse

router = APIRouter(prefix="/api/targets", tags=["targets"], dependencies=[Depends(require_token)])


@router.get("")
async def list_targets(session: Session) -> list[TargetResponse]:
    """Every target sharpwire knows, present in NetBox or not, with its open plan if it has drifted."""
    open_plans = dict((await session.execute(select(Plan.target_id, Plan.id).where(Plan.status == "open"))).all())
    rows = await session.scalars(select(Target).order_by(Target.kind, Target.name))
    return [TargetResponse.model_validate(t).model_copy(update={"open_plan_id": open_plans.get(t.id)}) for t in rows]
