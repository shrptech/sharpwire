from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs import queue
from app.models.job import Job


async def test_a_second_enqueue_joins_the_queued_job(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as s:
        assert await queue.enqueue(s, "reconcile") is True
        assert await queue.enqueue(s, "reconcile", delay_seconds=10) is False
    async with session_factory() as s:
        assert len((await s.scalars(select(Job))).all()) == 1


async def test_a_running_job_doesnt_block_a_new_one(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as s:
        await queue.enqueue(s, "reconcile")
        assert await queue.claim(s, 60) is not None
        assert await queue.enqueue(s, "reconcile") is True


async def test_claim_takes_only_due_jobs_and_skips_locked_ones(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as s:
        await queue.enqueue(s, "reconcile", delay_seconds=3600)
    async with session_factory.begin() as s:
        assert await queue.claim(s, 60) is None
        await s.execute(update(Job).values(run_after=datetime.now(UTC) - timedelta(seconds=1)))
    async with session_factory() as holder, holder.begin():
        held = await queue.claim(holder, 60)
        assert held is not None
        # Another worker, while the first still holds the row's lock, finds nothing to take.
        async with session_factory.begin() as other:
            assert await queue.claim(other, 60) is None


async def test_a_dead_workers_job_is_failed(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as s:
        await queue.enqueue(s, "reconcile")
        job = await queue.claim(s, -1)
        assert job is not None
    async with session_factory.begin() as s:
        assert await queue.expire_leases(s) == 1
    async with session_factory() as s:
        job = await s.get(Job, job.id)
        assert job is not None and job.status == "failed" and job.error
