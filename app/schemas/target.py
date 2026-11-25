import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import TargetKind


class TargetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: TargetKind
    address: str
    managed: bool
    present: bool
    last_reconciled_at: datetime | None
    last_error: str | None
    open_plan_id: uuid.UUID | None = None
