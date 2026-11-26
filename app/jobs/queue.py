"""The Postgres job queue: no Celery, no Redis (AGENTS.md).

A job is claimed with SELECT … FOR UPDATE SKIP LOCKED, so workers never block on one another, and is held under a
lease. A running job whose lease runs out belongs to a worker that died: it's failed, and the scheduler queues the
work again. Each function takes the session and leaves committing to the caller.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import JobKind
from app.models.job import Job


async def enqueue(
    session: AsyncSession, kind: JobKind, *, target_id: uuid.UUID | None = None, delay_seconds: float = 0
) -> bool:
    """Queue a job, unless the same kind for the same target is already queued: then that one covers this one.
    Returns whether a new job was queued.
    """
    run_after = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    statement = (
        insert(Job)
        .values(id=uuid.uuid4(), kind=kind, target_id=target_id, status="queued", run_after=run_after)
        .on_conflict_do_nothing(index_elements=[Job.kind, Job.target_id], index_where=text("status = 'queued'"))
        .returning(Job.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def claim(session: AsyncSession, lease_seconds: float) -> Job | None:
    """The next due job, now running under a lease, or None."""
    now = datetime.now(UTC)
    job = await session.scalar(
        select(Job)
        .where(Job.status == "queued", Job.run_after <= now)
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = now
    job.attempts += 1
    job.lease_until = now + timedelta(seconds=lease_seconds)
    await session.flush()
    return job


async def finish(session: AsyncSession, job: Job, error: str | None = None) -> None:
    """Record how a running job ended. One whose lease ran out was already failed by another worker's tick, and
    another run may be under way: that record stands.
    """
    if job.status != "running":
        return
    job.status = "failed" if error else "done"
    job.error = error
    job.finished_at = datetime.now(UTC)
    job.lease_until = None
    await session.flush()


async def expire_leases(session: AsyncSession) -> int:
    """Fail running jobs whose worker died. Returns how many."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(Job)
        .where(Job.status == "running", Job.lease_until < now)
        .values(
            status="failed",
            error="The worker running this job stopped. It runs again on schedule.",
            finished_at=now,
            lease_until=None,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def renew(session: AsyncSession, job_id: uuid.UUID, lease_seconds: float) -> None:
    """Extend a running job's lease, so a long run isn't taken for a dead worker's."""
    await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running")
        .values(lease_until=datetime.now(UTC) + timedelta(seconds=lease_seconds))
    )


async def last_created(session: AsyncSession, kind: JobKind) -> datetime | None:
    """When a job of this kind was last queued, whatever became of it."""
    return await session.scalar(select(Job.created_at).where(Job.kind == kind).order_by(Job.created_at.desc()).limit(1))
