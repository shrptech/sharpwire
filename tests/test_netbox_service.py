import json
import re

import httpx
import pytest

from app.services.netbox import LIST_QUERIES, PAGE_SIZE, NetBoxError, fetch_intent_snapshot

URL = "https://netbox.example.invalid"
TOKEN = "nbt_key.secret"


def client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _list_and_offset(request: httpx.Request) -> tuple[str, int]:
    query = json.loads(request.content)["query"]
    key = next(k for k in LIST_QUERIES if re.search(rf"\b{k}\b", query))
    offset = re.search(r"offset: (\d+)", query)
    assert offset is not None
    return key, int(offset.group(1))


async def test_reads_every_list_page_by_page_with_a_bearer_token() -> None:
    # NetBox's MAX_PAGE_SIZE may be lower than PAGE_SIZE: here it returns two rows per page.
    rows = {key: [{"n": i} for i in range(5)] for key in LIST_QUERIES}
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key, offset = _list_and_offset(request)
        return httpx.Response(200, json={"data": {key: rows[key][offset : offset + 2]}})

    async with client(httpx.MockTransport(handle)) as c:
        snapshot = await fetch_intent_snapshot(URL + "/", TOKEN, c)

    assert snapshot == rows
    assert {str(r.url) for r in seen} == {f"{URL}/graphql/"}
    assert {r.headers["Authorization"] for r in seen} == {f"Bearer {TOKEN}"}
    assert len(seen) == len(LIST_QUERIES) * 4  # three pages of rows and an empty one, per list


def test_every_list_reads_only_fabric_objects() -> None:
    for key, query in LIST_QUERIES.items():
        assert 'tags: {slug: {exact: "sharpwire-managed"}}' in query, key
        if key != "asn_list":
            assert "status: {exact: STATUS_ACTIVE}" in query, key
    assert f"limit: {PAGE_SIZE}" in LIST_QUERIES["device_list"].replace("{page}", f"limit: {PAGE_SIZE}")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(403, json={"detail": "Invalid token"}), "Check NETBOX_URL and NETBOX_TOKEN."),
        (httpx.Response(200, text="<html>"), "Check NETBOX_URL and NETBOX_TOKEN."),
        (httpx.Response(200, json=[1, 2]), "returned something unexpected"),
        (httpx.Response(200, json={"data": None, "errors": [{"message": "x"}]}), "NetBox rejected the intent query."),
        (httpx.Response(200, json={"data": None}), "NetBox returned no data"),
        (httpx.Response(200, json={"data": {"other": []}}), "NetBox returned no data"),
    ],
)
async def test_errors_say_what_to_check(response: httpx.Response, message: str) -> None:
    async with client(httpx.MockTransport(lambda _: response)) as c:
        with pytest.raises(NetBoxError, match=message):
            await fetch_intent_snapshot(URL, TOKEN, c)


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, json={"data": None, "errors": [{"message": "bad token nbt_x"}]})],
)
async def test_the_token_is_never_logged(response: httpx.Response, caplog: pytest.LogCaptureFixture) -> None:
    async with client(httpx.MockTransport(lambda _: response)) as c:
        with pytest.raises(NetBoxError):
            await fetch_intent_snapshot(URL, TOKEN, c)
    assert caplog.records
    assert "secret" not in caplog.text
