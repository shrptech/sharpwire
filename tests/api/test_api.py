"""The read-only JSON API and NetBox's webhook."""

import copy
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api._deps import get_session
from app.config import get_settings
from app.jobs import reconcile
from app.main import create_app
from app.models.job import Job
from tests.jobs.test_reconcile import Devices, sources

CAPTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "routeros" / "core-a.live.json").read_text())
TOKEN = {"Authorization": f"Bearer {get_settings().api_token}"}


@pytest.fixture
async def api(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def drifted(session_factory: async_sessionmaker[AsyncSession]) -> None:
    devices = Devices()
    devices.live["core-a"] = copy.deepcopy(CAPTURE["live"])
    await reconcile.run(session_factory, sources(devices))


@pytest.mark.parametrize("path", ["/api/targets", "/api/plans", "/api/alerts"])
async def test_the_api_needs_the_token(api: httpx.AsyncClient, path: str) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = await api.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Send the API token as a Bearer token.", "error_code": "unauthorized"}


async def test_targets_show_their_open_plan(api: httpx.AsyncClient, drifted: None) -> None:
    targets = (await api.get("/api/targets", headers=TOKEN)).json()
    by_name = {t["name"]: t for t in targets}
    assert [t["name"] for t in targets] == ["core-a", "core-b", "leaf-a", "leaf-b"]
    assert by_name["core-a"]["open_plan_id"] is not None and by_name["core-b"]["open_plan_id"] is None


async def test_open_plans_carry_the_diff_and_ops(api: httpx.AsyncClient, drifted: None) -> None:
    page = (await api.get("/api/plans", params={"status": "open"}, headers=TOKEN)).json()
    assert page["total"] == 1
    plan = page["items"][0]
    assert plan["diff"].startswith("~ /interface/ethernet name=sfp-sfpplus1: l2mtu")
    assert plan["ops"][0]["action"] == "set" and plan["inverse_ops"][0]["fields"] == {"l2mtu": None}

    one = await api.get(f"/api/plans/{plan['id']}", headers=TOKEN)
    assert one.json()["hash"] == plan["hash"]


async def test_a_missing_plan_is_a_404_with_an_error_code(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/plans/00000000-0000-0000-0000-000000000000", headers=TOKEN)
    assert response.status_code == 404
    assert response.json() == {"detail": "There's no plan with that ID.", "error_code": "plan_not_found"}


async def test_alerts_list_open_ones(api: httpx.AsyncClient, drifted: None) -> None:
    page = (await api.get("/api/alerts", headers=TOKEN)).json()
    assert page["total"] == 1 and page["items"][0]["topic"] == "target:core-a:drift"
    assert (await api.get("/api/alerts", params={"resolved": True}, headers=TOKEN)).json()["total"] == 0


def signed(body: bytes, secret: str | None = None) -> dict[str, str]:
    key = (secret or get_settings().netbox_webhook_secret).encode()
    return {"X-Hook-Signature": hmac.new(key, body, hashlib.sha512).hexdigest(), "Content-Type": "application/json"}


async def test_a_signed_netbox_change_queues_one_debounced_reconcile(
    api: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    body = json.dumps({"event": "updated", "model": "interface", "data": {"id": 1}}).encode()
    first = await api.post("/hooks/netbox", content=body, headers=signed(body))
    second = await api.post("/hooks/netbox", content=body, headers=signed(body))
    assert (first.status_code, first.json(), second.json()) == (202, {"queued": True}, {"queued": False})
    async with session_factory() as s:
        (job,) = (await s.scalars(select(Job))).all()
    assert (job.run_after - job.created_at).total_seconds() >= get_settings().netbox_debounce_seconds - 1


async def test_an_unsigned_or_wrongly_signed_call_is_refused(
    api: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    body = b'{"event": "updated"}'
    for headers in ({}, signed(body, secret="not-the-secret"), signed(b"other body")):
        response = await api.post("/hooks/netbox", content=body, headers=headers)
        assert response.status_code == 401 and response.json()["error_code"] == "bad_signature"
    async with session_factory() as s:
        assert (await s.scalars(select(Job))).all() == []


async def test_a_non_ascii_signature_is_refused_not_a_500(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/hooks/netbox", content=b"{}", headers={b"X-Hook-Signature": ("é" * 128).encode("latin-1")}
    )
    assert response.status_code == 401 and response.json()["error_code"] == "bad_signature"


async def test_a_refusal_names_the_scheme_and_bad_input_keeps_the_error_shape(api: httpx.AsyncClient) -> None:
    refused = await api.get("/api/plans")
    assert refused.headers["WWW-Authenticate"] == "Bearer"
    invalid = await api.get("/api/plans", params={"status": "bogus"}, headers=TOKEN)
    assert invalid.status_code == 422
    body = invalid.json()
    assert body["error_code"] == "invalid_request" and body["detail"].startswith(
        "The request isn't valid: query.status"
    )


async def test_the_schema_isnt_served(api: httpx.AsyncClient) -> None:
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert (await api.get(path)).status_code == 404
