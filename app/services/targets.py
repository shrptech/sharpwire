"""Targets, synced from the intent: every RouterOS device in NetBox's fabric (Proxmox targets come in 3d to 3f)."""

from collections.abc import Sequence
from datetime import datetime
from ipaddress import IPv6Interface

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import TargetKind
from app.intent.models import Network
from app.models.target import Target


def _mgmt(network: Network) -> list[tuple[str, TargetKind, str, bool]]:
    """(name, kind, mgmt address, managed) per device. The model guarantees each has a mgmt IPv6 address."""
    out: list[tuple[str, TargetKind, str, bool]] = []
    for core in network.cores:
        address = core.vlan_interface(network.fabric.mgmt_vlan_id).address(6)
        assert isinstance(address, IPv6Interface)
        out.append((core.name, "core", str(address.ip), core.managed))
    for leaf in network.leafs:
        address = leaf.mgmt.address(6)
        assert isinstance(address, IPv6Interface)
        out.append((leaf.name, "leaf", str(address.ip), leaf.managed))
    return out


async def sync(session: AsyncSession, network: Network) -> Sequence[Target]:
    """Make the targets table match the intent. Devices no longer in it stay, as not present. Returns the present
    targets, in the intent's order.
    """
    existing = {t.name: t for t in await session.scalars(select(Target).with_for_update())}
    present: list[Target] = []
    for name, kind, address, managed in _mgmt(network):
        target = existing.pop(name, None)
        if target is None:
            target = Target(name=name, kind=kind, address=address, managed=managed, present=True)
            session.add(target)
        else:
            target.kind, target.address, target.managed, target.present = kind, address, managed, True
        present.append(target)
    for gone in existing.values():
        gone.present = False
    await session.flush()
    return present


async def not_planned(session: AsyncSession) -> Sequence[Target]:
    """Targets that left NetBox or aren't managed there: their plans close and their alerts are withdrawn."""
    rows = await session.scalars(select(Target).where((Target.present.is_(False)) | (Target.managed.is_(False))))
    return list(rows)


async def overdue(session: AsyncSession, before: datetime) -> Sequence[Target]:
    """Managed, present targets with no successful reconcile since `before` (and none at all if older than it)."""
    rows = await session.scalars(
        select(Target).where(
            Target.present.is_(True),
            Target.managed.is_(True),
            func.coalesce(Target.last_reconciled_at, Target.created_at) < before,
        )
    )
    return list(rows)
