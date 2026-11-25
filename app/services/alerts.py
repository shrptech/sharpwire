"""Alerts, with sharpstack's outbox semantics (docs/07-security-and-observability.md#alerts).

- A **dedup key** names one condition. Raising it again while its alert is open updates that alert (message,
  severity, `last_seen_at`, `count`) instead of adding one.
- A **topic** names what the alert is about (`target:core-a:drift`). Raising a new dedup key on a topic supersedes
  the topic's other open alerts: a newer plan's drift alert replaces the older plan's.
- **Withdrawing** a topic prefix resolves every open alert under it, once the condition clears.

Topics used:
- `target:<name>:drift`, keyed by plan hash: the target differs from NetBox;
- `target:<name>:read`: the target couldn't be read or planned;
- `target:<name>:overdue`: no successful reconcile for a while;
- `netbox:read`: the intent couldn't be built.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AlertSeverity
from app.models.alert import Alert


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def raise_alert(
    session: AsyncSession,
    *,
    dedup_key: str,
    topic: str,
    severity: AlertSeverity,
    message: str,
    target_id: uuid.UUID | None = None,
    plan_id: uuid.UUID | None = None,
) -> Alert:
    now = datetime.now(UTC)
    await session.execute(
        update(Alert)
        .where(Alert.topic == topic, Alert.dedup_key != dedup_key, Alert.resolved_at.is_(None))
        .values(resolved_at=now)
    )
    statement = (
        insert(Alert)
        .values(
            id=uuid.uuid4(),
            dedup_key=dedup_key,
            topic=topic,
            severity=severity,
            message=message,
            target_id=target_id,
            plan_id=plan_id,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=[Alert.dedup_key],
            index_where=Alert.resolved_at.is_(None),
            set_={
                "message": message,
                "severity": severity,
                "plan_id": plan_id,
                "last_seen_at": now,
                "count": Alert.count + 1,
            },
        )
        .returning(Alert.id)
    )
    alert_id = (await session.execute(statement)).scalar_one()
    alert = await session.get(Alert, alert_id, populate_existing=True)
    assert alert is not None
    return alert


async def withdraw(session: AsyncSession, topic_prefix: str) -> int:
    """Resolve every open alert whose topic starts with `topic_prefix`. Returns how many."""
    result = await session.execute(
        update(Alert)
        .where(Alert.topic.like(f"{_escape_like(topic_prefix)}%", escape="\\"), Alert.resolved_at.is_(None))
        .values(resolved_at=datetime.now(UTC))
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def open_alerts(session: AsyncSession, topic_prefix: str = "") -> list[Alert]:
    rows = await session.scalars(
        select(Alert)
        .where(Alert.resolved_at.is_(None), Alert.topic.like(f"{_escape_like(topic_prefix)}%", escape="\\"))
        .order_by(Alert.created_at, func.coalesce(Alert.topic, ""))
    )
    return list(rows)
