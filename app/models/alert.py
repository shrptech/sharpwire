import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import AlertSeverity
from app.models._constants import check_in
from app.models._mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class Alert(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An alert, with sharpstack's outbox semantics (app/services/alerts.py): a repeat of an open alert's dedup key
    updates it, a new alert supersedes the open ones on its topic, and a topic is withdrawn once its condition clears.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        check_in("severity", AlertSeverity, "ck_alert_severity"),
        Index("uq_alerts_dedup_key", "dedup_key", unique=True, postgresql_where=text("resolved_at IS NULL")),
        # Withdrawal matches topics by prefix (`target:core-a:` retires every alert about core-a).
        Index(
            "ix_alerts_topic",
            "topic",
            postgresql_ops={"topic": "text_pattern_ops"},
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("ix_alerts_created_at", "created_at"),
    )

    dedup_key: Mapped[str] = mapped_column(String, nullable=False)
    topic: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("targets.id"))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
