"""A reconcile of the example network, from its NetBox snapshot, against simulated and captured devices."""

import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
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
    assert overdue.severity == "critical" and "hasn't been planned ever" in overdue.message

    devices.down.clear()
    await reconcile.run(session_factory, sources(devices))
    assert (await state(session_factory))[2] == []
