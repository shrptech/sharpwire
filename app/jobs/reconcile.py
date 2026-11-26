"""The reconcile job: build the intent once, then read and plan every managed target (docs/06-drift-and-approvals.md).

It never writes to a target. A non-empty plan is drift: it's stored, and raises an alert until the target matches
NetBox again. One target that can't be read doesn't stop the others; NetBox that can't be read stops the run.
"""

import logging
import ssl
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.drivers.routeros.client import RouterOSClient, RouterOSError, base_url, tls_context
from app.drivers.routeros.diff import LiveState, LiveStateError
from app.drivers.routeros.plan import CORE_MENUS, LEAF_MENUS, plan_core, plan_leaf
from app.intent.builder import BuildError, build_network
from app.intent.models import Network
from app.models.target import Target
from app.schemas.plan import PlanDraft
from app.services import alerts, plans, targets
from app.services.netbox import NetBoxError, fetch_intent_snapshot, latest_change_id

logger = logging.getLogger(__name__)

# Reads one target's menus. The default talks RouterOS REST; tests pass their own.
Reader = Callable[[Target, Sequence[str]], Awaitable[LiveState]]


class ReconcileFailed(Exception):
    """The run couldn't build the intent. `detail` is safe to show."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Sources:
    snapshot: Callable[[], Awaitable[dict[str, Any]]]
    change_id: Callable[[], Awaitable[int | None]]
    read: Reader


def sources(settings: Settings) -> Sources:
    """The real NetBox and devices, from settings."""

    async def snapshot() -> dict[str, Any]:
        return await fetch_intent_snapshot(settings.netbox_url, settings.netbox_token)

    async def change_id() -> int | None:
        return await latest_change_id(settings.netbox_url, settings.netbox_token)

    async def read(target: Target, menus: Sequence[str]) -> LiveState:
        try:
            verify = tls_context(settings.routeros_ca_file)
        except (OSError, ssl.SSLError) as exc:
            logger.warning("RouterOS CA file %s unusable", settings.routeros_ca_file, exc_info=True)
            raise RouterOSError("The RouterOS CA file can't be read. Check ROUTEROS_CA_FILE.") from exc
        url = base_url(ip_address(target.address))
        async with RouterOSClient(url, settings.routeros_username, settings.routeros_password, verify) as client:
            return await client.read(menus)

    return Sources(snapshot=snapshot, change_id=change_id, read=read)


def _plan(network: Network, target: Target, live: LiveState) -> PlanDraft:
    if target.kind == "core":
        return plan_core(network, network.core_by_name[target.name], live)
    leaf = next(leaf for leaf in network.leafs if leaf.name == target.name)
    return plan_leaf(network, leaf, live)


MENUS = {"core": CORE_MENUS, "leaf": LEAF_MENUS}


async def _unreadable(session_factory: async_sessionmaker[AsyncSession], target: Target, detail: str) -> None:
    async with session_factory.begin() as session:
        current = await session.get(Target, target.id, with_for_update=True)
        assert current is not None
        current.last_error = detail
        await alerts.raise_alert(
            session,
            dedup_key=_topic(target, "read"),
            topic=_topic(target, "read"),
            severity="warning",
            message=f"sharpwire couldn't plan {target.name}: {detail}",
            target_id=target.id,
        )


def _topic(target: Target, what: str) -> str:
    return f"target:{target.name}:{what}"


async def _drift(session: AsyncSession, target: Target, draft: PlanDraft, change_id: int | None) -> None:
    plan = await plans.record(session, target, draft, change_id)
    if plan is None:
        await alerts.withdraw(session, _topic(target, "drift"))
        return
    count = len(plan.ops)
    message = f"{target.name} differs from NetBox: {count} change{'s' if count != 1 else ''} in plan {plan.id}."
    if plan.guard_violations:
        message += " The plan breaks a hard guard, so it can't be approved: make NetBox match the device."
    await alerts.raise_alert(
        session,
        dedup_key=f"{_topic(target, 'drift')}:{plan.hash}",
        topic=_topic(target, "drift"),
        severity="warning",
        message=message,
        target_id=target.id,
        plan_id=plan.id,
    )


# The session-level advisory lock one reconcile holds for its whole run ('reco').
RUN_LOCK = 0x7265636F


async def run(session_factory: async_sessionmaker[AsyncSession], source: Sources) -> dict[str, int]:
    """One reconcile of the whole fabric, with no other running. Returns counts for the job's log line.

    Runs are serialised on a session-level advisory lock, held on a connection of its own: two workers running at
    once would otherwise let an older run, reading older intent, overwrite a newer run's plans. A run waits for the
    one before it and only then reads NetBox, so the last run to finish has the newest intent.
    """
    engine = session_factory.kw["bind"]
    async with engine.connect() as lock:
        await lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": RUN_LOCK})
        await lock.commit()
        try:
            return await _run(session_factory, source)
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RUN_LOCK})
            await lock.commit()


async def _run(session_factory: async_sessionmaker[AsyncSession], source: Sources) -> dict[str, int]:
    change_id = await source.change_id()
    try:
        network = build_network(await source.snapshot())
    except (NetBoxError, BuildError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        async with session_factory.begin() as session:
            await alerts.raise_alert(
                session,
                dedup_key="netbox:read",
                topic="netbox:read",
                severity="warning",
                message=f"sharpwire couldn't build the intent from NetBox, so nothing was planned: {detail}",
            )
        raise ReconcileFailed(detail) from exc

    async with session_factory.begin() as session:
        await alerts.withdraw(session, "netbox:read")
        present = list(await targets.sync(session, network))
        for gone in await targets.not_planned(session):
            await plans.close(session, gone)
            await alerts.withdraw(session, f"target:{gone.name}:")
    counts = {"planned": 0, "drifted": 0, "unreadable": 0}

    for target in (t for t in present if t.managed):
        try:
            live = await source.read(target, MENUS[target.kind])
            draft = _plan(network, target, live)
        except (RouterOSError, LiveStateError) as exc:
            detail = getattr(exc, "detail", None) or " ".join(getattr(exc, "problems", [])) or str(exc)
            logger.warning("Reconcile of %s failed: %s", target.name, detail)
            await _unreadable(session_factory, target, detail)
            counts["unreadable"] += 1
            continue
        except Exception:
            # Anything else (a driver bug, a device answering nonsense) is this target's problem, not the run's.
            logger.exception("Reconcile of %s failed unexpectedly", target.name)
            await _unreadable(session_factory, target, "planning it failed unexpectedly. See the worker's log.")
            counts["unreadable"] += 1
            continue
        async with session_factory.begin() as session:
            current = await session.get(Target, target.id, with_for_update=True)
            assert current is not None
            await _drift(session, current, draft, change_id)
            current.last_reconciled_at = datetime.now(UTC)
            current.last_error = None
            await alerts.withdraw(session, _topic(current, "read"))
            await alerts.withdraw(session, _topic(current, "overdue"))
        counts["planned"] += 1
        counts["drifted"] += 0 if draft.empty else 1
    return counts
