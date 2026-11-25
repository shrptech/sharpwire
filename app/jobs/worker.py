"""The worker: runs queued jobs, and is the scheduler too (docs/06-drift-and-approvals.md).

    uv run python -m app.jobs.worker

Every tick it fails jobs whose worker died, queues a reconcile once one is due, and raises "reconcile overdue" for
targets nothing has planned in a while. Any number of workers can run: the queue's unique index makes a second
reconcile join the queued one, and SKIP LOCKED keeps them off one another's jobs.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.database import get_session_factory
from app.jobs import queue
from app.jobs import reconcile as reconcile_job
from app.models.job import Job
from app.services import alerts, targets

logger = logging.getLogger(__name__)

TICK_SECONDS = 15
IDLE_SECONDS = 1


async def tick(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    now = datetime.now(UTC)
    interval = timedelta(seconds=settings.reconcile_interval_seconds)
    async with session_factory.begin() as session:
        await queue.expire_leases(session)
        last = await queue.last_created(session, "reconcile")
        if last is None or last <= now - interval:
            await queue.enqueue(session, "reconcile")
        cutoff = now - interval * settings.reconcile_overdue_intervals
        for target in await targets.overdue(session, cutoff):
            since = target.last_reconciled_at
            when = f"since {since:%Y-%m-%d %H:%M} UTC" if since else "ever"
            await alerts.raise_alert(
                session,
                dedup_key=f"target:{target.name}:overdue",
                topic=f"target:{target.name}:overdue",
                severity="critical",
                message=f"{target.name} hasn't been planned {when}, so drift on it would go unseen.",
                target_id=target.id,
            )


async def run_one(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, source: reconcile_job.Sources
) -> Job | None:
    """Claim and run one due job. Returns it, finished, or None if nothing was due."""
    async with session_factory.begin() as session:
        job = await queue.claim(session, settings.job_lease_seconds)
    if job is None:
        return None
    error: str | None = None
    try:
        counts = await reconcile_job.run(session_factory, source)
        logger.info("reconcile %s: %s", job.id, counts)
    except reconcile_job.ReconcileFailed as exc:
        error = exc.detail
    except Exception:
        logger.exception("Job %s failed", job.id)
        error = "The job failed unexpectedly. See the worker's log."
    async with session_factory.begin() as session:
        current = await session.get(Job, job.id, with_for_update=True)
        assert current is not None
        await queue.finish(session, current, error)
    return current


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    session_factory = get_session_factory()
    source = reconcile_job.sources(settings)
    next_tick = 0.0
    loop = asyncio.get_running_loop()
    while True:
        if loop.time() >= next_tick:
            try:
                await tick(session_factory, settings)
            except Exception:
                logger.exception("Scheduler tick failed")
            next_tick = loop.time() + TICK_SECONDS
        try:
            ran = await run_one(session_factory, settings, source)
        except Exception:
            logger.exception("Claiming a job failed")
            ran = None
        if ran is None:
            await asyncio.sleep(IDLE_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
