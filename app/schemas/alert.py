import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import AlertSeverity


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    topic: str
    severity: AlertSeverity
    target_id: uuid.UUID | None
    plan_id: uuid.UUID | None
    message: str
    count: int
    created_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


class AlertPage(BaseModel):
    items: list[AlertResponse]
    total: int
