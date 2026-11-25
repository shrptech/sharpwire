import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import PlanStatus
from app.models._constants import check_in
from app.models._mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class Plan(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A driver's PlanDraft for one target, immutable once stored (docs/03-pipeline-and-validation.md#plan-lifecycle).

    Only non-empty plans are stored. A target has at most one open plan: a different one supersedes it, the same one
    (by hash) only moves `last_seen_at`, and an empty one supersedes it with nothing.
    """

    __tablename__ = "plans"
    __table_args__ = (
        check_in("status", PlanStatus, "ck_plan_status"),
        Index("uq_plans_target_id", "target_id", unique=True, postgresql_where=text("status = 'open'")),
        Index("ix_plans_target_id_created_at", "target_id", "created_at"),
    )

    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("targets.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open", server_default=text("'open'"))
    # sha256 over the target, its ops and the live-state fingerprint: equal plans have equal hashes.
    hash: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    # The NetBox changelog's newest entry when the intent was read; None if NetBox didn't say.
    netbox_change_id: Mapped[int | None] = mapped_column(BigInteger)
    ops: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    inverse_ops: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    diff: Mapped[str] = mapped_column(Text, nullable=False)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    guard_violations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    unmanaged: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
