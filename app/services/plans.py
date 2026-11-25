"""Storing drivers' plans: one open plan per target (docs/03-pipeline-and-validation.md#plan-lifecycle)."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan import Plan
from app.models.target import Target
from app.schemas.plan import PlanDraft


def plan_hash(target: str, draft: PlanDraft) -> str:
    """Equal for equal plans: the same target, ops and live state."""
    body = {"target": target, "fingerprint": draft.fingerprint, "ops": [op.model_dump() for op in draft.ops]}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


async def open_plan(session: AsyncSession, target: Target) -> Plan | None:
    return await session.scalar(
        select(Plan).where(Plan.target_id == target.id, Plan.status == "open").with_for_update()
    )


async def record(session: AsyncSession, target: Target, draft: PlanDraft, netbox_change_id: int | None) -> Plan | None:
    """Store `draft` as the target's open plan, and return it; an empty draft closes the open plan and returns None.

    A draft equal to the open plan (by hash) keeps that plan, so the same drift seen every 5 minutes is one plan.
    """
    # Serialise per target: two reconciles at once must not both supersede and both insert.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('plan:' || :target))"), {"target": str(target.id)}
    )
    now = datetime.now(UTC)
    current = await open_plan(session, target)
    digest = plan_hash(target.name, draft)
    if current is not None and current.hash == digest:
        current.last_seen_at = now
        return current
    if current is not None:
        current.status = "superseded"
        current.superseded_at = now
        await session.flush()
    if draft.empty:
        return None
    plan = Plan(
        target_id=target.id,
        hash=digest,
        fingerprint=draft.fingerprint,
        netbox_change_id=netbox_change_id,
        ops=[op.model_dump() for op in draft.ops],
        inverse_ops=[op.model_dump() for op in draft.inverse_ops],
        diff=draft.diff,
        warnings=[w.model_dump() for w in draft.warnings],
        guard_violations=list(draft.guard_violations),
        unmanaged=[u.model_dump() for u in draft.unmanaged],
        last_seen_at=now,
    )
    session.add(plan)
    await session.flush()
    return plan


async def count_open(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(Plan).where(Plan.status == "open")) or 0)


async def close(session: AsyncSession, target: Target) -> None:
    """Supersede the target's open plan with nothing, as when it stops being planned."""
    current = await open_plan(session, target)
    if current is not None:
        current.status = "superseded"
        current.superseded_at = datetime.now(UTC)
        await session.flush()
