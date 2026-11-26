"""A reconcile of the example network, from its NetBox snapshot, against simulated and captured devices."""

import asyncio
import copy
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.drivers.routeros.client import RouterOSError
from app.drivers.routeros.diff import LiveState
from app.drivers.routeros.render import render_core, render_leaf
from app.jobs import reconcile, worker
from app.models.job import Job
from app.models.plan import Plan
from app.models.target import Target
from app.services import alerts
from app.services.netbox import NetBoxError
from tests.drivers.routeros_device import device_view
from tests.fixtures import netbox
from tests.fixtures.example_network import example_network

NETWORK = example_network()
CAPTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "routeros" / "core-a.live.json").read_text())


class Devices:
    """Every device as it would read back in sync, unless a test changes it."""

    def __init__(self) -> None:
        self.live: dict[str, Any] = {c.name: device_view(render_core(NETWORK, c)) for c in NETWORK.cores}
        self.live |= {leaf.name: device_view(render_leaf(NETWORK, leaf)) for leaf in NETWORK.leafs}
        self.down: set[str] = set()
        self.reads: list[str] = []

    async def read(self, target: Target, menus: Sequence[str]) -> LiveState:
        self.reads.append(target.name)
        if target.name in self.down:
            raise RouterOSError(f"https://[{target.address}]:443/rest didn't answer (ConnectTimeout).")
        return copy.deepcopy(self.live[target.name])


def sources(devices: Devices, snapshot: dict[str, Any] | None = None, netbox_down: bool = False) -> reconcile.Sources:
    async def fetch() -> dict[str, Any]:
        if netbox_down:
            raise NetBoxError("NetBox's GraphQL API couldn't be read. Check NETBOX_URL and NETBOX_TOKEN.")
        return snapshot if snapshot is not None else netbox.load("example_network")

    async def change_id() -> int | None:
        return 4242

    return reconcile.Sources(snapshot=fetch, change_id=change_id, read=devices.read)


async def state(factory: async_sessionmaker[AsyncSession]) -> tuple[dict[str, Target], list[Plan], list[str]]:
    async with factory() as s:
        targets = {t.name: t for t in await s.scalars(select(Target))}
        plans = list(await s.scalars(select(Plan).order_by(Plan.created_at)))
        topics = sorted(a.topic for a in await alerts.open_alerts(s))
    return targets, plans, topics


async def test_a_fabric_in_sync_has_targets_and_nothing_else(session_factory: async_sessionmaker[AsyncSession]) -> None:
    devices = Devices()
    counts = await reconcile.run(session_factory, sources(devices))

    targets, plans, topics = await state(session_factory)
    assert sorted(targets) == ["core-a", "core-b", "leaf-a", "leaf-b"]
    assert targets["core-a"].address == "2001:db8:100::2" and targets["core-a"].kind == "core"
    assert all(t.last_reconciled_at is not None for t in targets.values())
    assert plans == [] and topics == []
    assert counts == {"planned": 4, "drifted": 0, "unreadable": 0}


async def test_drift_is_one_plan_and_one_alert_however_often_its_seen(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    devices = Devices()
    devices.live["core-a"] = copy.deepcopy(CAPTURE["live"])  # the CHR: l2mtu differs

    for _ in range(3):
        await reconcile.run(session_factory, sources(devices))

    _, plans, topics = await state(session_factory)
    assert len(plans) == 1 and plans[0].status == "open" and plans[0].netbox_change_id == 4242
    assert plans[0].diff.count("l2mtu") == 2
    assert topics == ["target:core-a:drift"]
    async with session_factory() as s:
        (alert,) = await alerts.open_alerts(s)
    assert alert.count == 3 and alert.plan_id == plans[0].id
    assert alert.message == f"core-a differs from NetBox: 2 changes in plan {plans[0].id}."


async def test_new_drift_supersedes_the_plan_and_its_alert(session_factory: async_sessionmaker[AsyncSession]) -> None:
    devices = Devices()
    devices.live["core-a"] = copy.deepcopy(CAPTURE["live"])
    await reconcile.run(session_factory, sources(devices))
    conn = next(r for r in devices.live["core-a"]["routing/bgp/connection"] if r["name"] == "pub6")
    conn["use-bfd"] = "false"
    await reconcile.run(session_factory, sources(devices))

    _, plans, topics = await state(session_factory)
    assert [p.status for p in plans] == ["superseded", "open"]
    assert topics == ["target:core-a:drift"]
    async with session_factory() as s:
        (alert,) = await alerts.open_alerts(s)
    assert alert.plan_id == plans[1].id


async def test_a_target_back_in_sync_closes_its_plan_and_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    devices = Devices()
    devices.live["core-a"] = copy.deepcopy(CAPTURE["live"])
    await reconcile.run(session_factory, sources(devices))
    devices.live["core-a"] = device_view(render_core(NETWORK, NETWORK.core_by_name["core-a"]))
    await reconcile.run(session_factory, sources(devices))

    _, plans, topics = await state(session_factory)
    assert [p.status for p in plans] == ["superseded"] and topics == []


async def test_a_guard_violation_says_the_plan_cant_be_approved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    devices = Devices()
    address = next(r for r in devices.live["core-a"]["ipv6/address"] if r["interface"] == "vlan10")
    address["address"] = "2001:db8:100::99/64"
    await reconcile.run(session_factory, sources(devices))

    _, plans, _ = await state(session_factory)
    assert plans[0].guard_violations
    async with session_factory() as s:
        (alert,) = await alerts.open_alerts(s)
    assert "breaks a hard guard" in alert.message


async def test_an_unreadable_target_doesnt_stop_the_others(session_factory: async_sessionmaker[AsyncSession]) -> None:
    devices = Devices()
    devices.down.add("core-b")
    counts = await reconcile.run(session_factory, sources(devices))

    targets, _, topics = await state(session_factory)
    assert counts == {"planned": 3, "drifted": 0, "unreadable": 1}
    assert topics == ["target:core-b:read"]
    assert targets["core-b"].last_error and "didn't answer" in targets["core-b"].last_error
    assert targets["core-b"].last_reconciled_at is None

    devices.down.clear()
    await reconcile.run(session_factory, sources(devices))
    targets, _, topics = await state(session_factory)
    assert topics == [] and targets["core-b"].last_error is None


async def test_netbox_down_fails_the_run_and_alerts(session_factory: async_sessionmaker[AsyncSession]) -> None:
    devices = Devices()
    with pytest.raises(reconcile.ReconcileFailed):
        await reconcile.run(session_factory, sources(devices, netbox_down=True))
    _, _, topics = await state(session_factory)
    assert topics == ["netbox:read"] and devices.reads == []

    await reconcile.run(session_factory, sources(devices))
    assert (await state(session_factory))[2] == []


async def test_a_device_leaving_netbox_closes_its_plan_and_alerts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    devices = Devices()
    devices.live["leaf-b"]["interface/bonding"][0]["mtu"] = "1500"
    await reconcile.run(session_factory, sources(devices))
    assert (await state(session_factory))[2] == ["target:leaf-b:drift"]

    snapshot = netbox.load("example_network")
    for device in snapshot["device_list"]:
        if device["name"] == "leaf-b":
            device["custom_fields"] = {**(device.get("custom_fields") or {}), "sharpwire_managed": False}
    await reconcile.run(session_factory, sources(devices, snapshot=snapshot))

    targets, plans, topics = await state(session_factory)
    assert targets["leaf-b"].managed is False
    assert [p.status for p in plans] == ["superseded"] and topics == []
    assert devices.reads.count("leaf-b") == 1


# --- The worker ---------------------------------------------------------------------------------------------------


async def test_the_scheduler_queues_one_reconcile_per_interval(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = get_settings()
    await worker.tick(session_factory, settings)
    await worker.tick(session_factory, settings)
    async with session_factory() as s:
        jobs = (await s.scalars(select(Job))).all()
    assert [j.kind for j in jobs] == ["reconcile"]


async def test_the_worker_runs_a_job_and_records_how_it_went(session_factory: async_sessionmaker[AsyncSession]) -> None:
    settings = get_settings()
    devices = Devices()
    await worker.tick(session_factory, settings)
    job = await worker.run_one(session_factory, settings, sources(devices))
    assert job is not None and job.status == "done" and job.error is None
    assert await worker.run_one(session_factory, settings, sources(devices)) is None

    async with session_factory.begin() as s:
        from app.jobs import queue

        await queue.enqueue(s, "reconcile")
    job = await worker.run_one(session_factory, settings, sources(devices, netbox_down=True))
    assert job is not None and job.status == "failed" and job.error and "NETBOX_URL" in job.error


async def test_a_target_not_planned_for_a_while_is_overdue(session_factory: async_sessionmaker[AsyncSession]) -> None:
    devices = Devices()
    devices.down.add("core-a")
    await reconcile.run(session_factory, sources(devices))
    settings = get_settings().model_copy(update={"reconcile_interval_seconds": 0})
    await worker.tick(session_factory, settings)

    _, _, topics = await state(session_factory)
    assert "target:core-a:overdue" in topics
    async with session_factory() as s:
        overdue = next(a for a in await alerts.open_alerts(s) if a.topic.endswith("overdue"))
    assert overdue.severity == "critical" and "hasn't been planned since it became managed" in overdue.message

    devices.down.clear()
    await reconcile.run(session_factory, sources(devices))
    assert (await state(session_factory))[2] == []


async def test_a_target_whose_plan_crashes_doesnt_stop_the_others(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = reconcile._plan

    def crash_on_core_a(network: Any, target: Target, live: LiveState) -> Any:
        if target.name == "core-a":
            raise AttributeError("'list' object has no attribute 'get'")
        return real(network, target, live)

    monkeypatch.setattr(reconcile, "_plan", crash_on_core_a)
    counts = await reconcile.run(session_factory, sources(Devices()))

    targets, _, topics = await state(session_factory)
    assert counts == {"planned": 3, "drifted": 0, "unreadable": 1}
    assert topics == ["target:core-a:read"]
    assert targets["core-a"].last_error == "planning it failed unexpectedly. See the worker's log."
    assert targets["core-b"].last_reconciled_at is not None


async def test_two_runs_at_once_take_turns(session_factory: async_sessionmaker[AsyncSession]) -> None:
    devices = Devices()
    inside = 0
    overlap = False
    real = devices.read

    async def slow_read(target: Target, menus: Sequence[str]) -> LiveState:
        nonlocal inside, overlap
        inside += 1
        overlap |= inside > 1
        await asyncio.sleep(0.02)
        inside -= 1
        return await real(target, menus)

    devices.read = slow_read  # type: ignore[method-assign]
    await asyncio.gather(
        reconcile.run(session_factory, sources(devices)), reconcile.run(session_factory, sources(devices))
    )
    assert not overlap
    assert len(devices.reads) == 8


async def test_a_device_removed_from_netbox_is_no_longer_present(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.intent.models import Network
    from app.services import targets as target_service

    async with session_factory.begin() as s:
        await target_service.sync(s, NETWORK)
    smaller = Network.model_construct(
        fabric=NETWORK.fabric, cores=NETWORK.cores[:1], leafs=NETWORK.leafs, nodes=NETWORK.nodes
    )
    async with session_factory.begin() as s:
        present = await target_service.sync(s, smaller)
        gone = await target_service.not_planned(s)
    assert "core-b" not in {t.name for t in present}
    assert [t.name for t in gone] == ["core-b"] and gone[0].present is False


async def test_the_scheduler_waits_an_interval_after_a_run_and_then_queues_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = get_settings()
    await worker.tick(session_factory, settings)
    await worker.run_one(session_factory, settings, sources(Devices()))
    await worker.tick(session_factory, settings)
    async with session_factory() as s:
        assert [j.status for j in await s.scalars(select(Job))] == ["done"]

    async with session_factory.begin() as s:
        await s.execute(
            update(Job).values(
                created_at=datetime.now(UTC) - timedelta(seconds=settings.reconcile_interval_seconds + 5)
            )
        )
    await worker.tick(session_factory, settings)
    async with session_factory() as s:
        assert sorted(j.status for j in await s.scalars(select(Job))) == ["done", "queued"]


async def test_overdue_is_raised_once_and_a_returning_target_gets_its_grace_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    devices = Devices()
    devices.down.add("core-a")
    await reconcile.run(session_factory, sources(devices))
    async with session_factory.begin() as s:
        await s.execute(update(Target).values(planned_since=datetime.now(UTC) - timedelta(days=1)))
    for _ in range(3):
        await worker.tick(session_factory, get_settings())
    async with session_factory() as s:
        (overdue,) = [a for a in await alerts.open_alerts(s) if a.topic.endswith("overdue")]
    assert overdue.count == 1

    # Unmanaged for a long while, then managed again: overdue counts from its return, not from long ago.
    snapshot = netbox.load("example_network")
    for device in snapshot["device_list"]:
        if device["name"] == "core-a":
            device["custom_fields"] = {**(device.get("custom_fields") or {}), "sharpwire_managed": False}
    await reconcile.run(session_factory, sources(devices, snapshot=snapshot))
    await reconcile.run(session_factory, sources(devices))
    await worker.tick(session_factory, get_settings())
    _, _, topics = await state(session_factory)
    assert topics == ["target:core-a:read"]


async def test_a_long_job_keeps_its_lease_and_a_stopped_worker_records_its_job(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings().model_copy(update={"job_lease_seconds": 0.3})
    started = asyncio.Event()

    async def slow_run(*_args: Any) -> dict[str, int]:
        started.set()
        # Past the original lease, another worker's tick looks for dead workers' jobs.
        await asyncio.sleep(0.5)
        await worker.tick(session_factory, settings)
        await asyncio.sleep(0.1)
        return {}

    monkeypatch.setattr(reconcile, "run", slow_run)
    await worker.tick(session_factory, settings)
    job = await worker.run_one(session_factory, settings, sources(Devices()))
    # Longer than its lease, but renewed meanwhile: a tick during the run didn't take it for a dead worker's.
    assert job is not None and job.status == "done"

    async def hang(*_args: Any) -> dict[str, int]:
        started.set()
        await asyncio.sleep(60)
        return {}

    monkeypatch.setattr(reconcile, "run", hang)
    async with session_factory.begin() as s:
        from app.jobs import queue

        await queue.enqueue(s, "reconcile")
    started.clear()
    task = asyncio.create_task(worker.run_one(session_factory, settings, sources(Devices())))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with session_factory() as s:
        stopped = (await s.scalars(select(Job).where(Job.status == "failed"))).one()
    assert stopped.error == "The worker stopped during the run. It runs again on schedule."
