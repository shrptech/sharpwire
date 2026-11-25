from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import alerts


async def test_a_repeat_updates_the_open_alert(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as s:
        first = await alerts.raise_alert(s, dedup_key="k", topic="target:a:drift", severity="warning", message="one")
    async with session_factory.begin() as s:
        again = await alerts.raise_alert(s, dedup_key="k", topic="target:a:drift", severity="critical", message="two")
    assert again.id == first.id
    assert (again.count, again.message, again.severity) == (2, "two", "critical")


async def test_a_new_key_supersedes_the_topic(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as s:
        old = await alerts.raise_alert(s, dedup_key="plan1", topic="target:a:drift", severity="warning", message="x")
        await alerts.raise_alert(s, dedup_key="other", topic="target:b:drift", severity="warning", message="x")
        new = await alerts.raise_alert(s, dedup_key="plan2", topic="target:a:drift", severity="warning", message="y")
    async with session_factory() as s:
        open_ = {a.dedup_key for a in await alerts.open_alerts(s)}
        assert open_ == {"other", "plan2"}
        assert new.id != old.id


async def test_a_resolved_key_raised_again_is_a_new_alert(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as s:
        first = await alerts.raise_alert(s, dedup_key="k", topic="target:a:read", severity="warning", message="x")
        await alerts.withdraw(s, "target:a:")
        second = await alerts.raise_alert(s, dedup_key="k", topic="target:a:read", severity="warning", message="x")
    assert second.id != first.id and second.count == 1


async def test_withdraw_matches_a_literal_prefix(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory.begin() as s:
        await alerts.raise_alert(s, dedup_key="1", topic="target:core_a:drift", severity="warning", message="x")
        await alerts.raise_alert(s, dedup_key="2", topic="target:coreXa:drift", severity="warning", message="x")
        assert await alerts.withdraw(s, "target:core_a:") == 1
    async with session_factory() as s:
        assert [a.dedup_key for a in await alerts.open_alerts(s)] == ["2"]
