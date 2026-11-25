import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import JobKind, JobStatus
from app.models._constants import check_in
from app.models._mixins import CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin


class Job(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """The Postgres job queue: workers claim rows with SELECT … FOR UPDATE SKIP LOCKED (app/jobs/queue.py)."""

    __tablename__ = "jobs"
    __table_args__ = (
        check_in("kind", JobKind, "ck_job_kind"),
        check_in("status", JobStatus, "ck_job_status"),
        Index("ix_jobs_status_run_after", "status", "run_after"),
        # One queued job per kind and target (NULL, the whole fabric, counts as one value): a second enqueue joins
        # the first, which is how NetBox's bursts of changes are debounced.
        Index(
            "uq_jobs_kind_target_id",
            "kind",
            "target_id",
            unique=True,
            postgresql_where=text("status = 'queued'"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued", server_default=text("'queued'"))
    # None: the job covers every target.
    target_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("targets.id"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A running job whose lease has passed belongs to a worker that died; it's queued again.
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String)
