"""The intent builder against a live NetBox 4.7 with the sharpwire plugin.

Run with `pytest -m integration tests/integration`, with NETBOX_URL and NETBOX_TOKEN pointing at it. Against the dev NetBox seeded with the network doc's example
(netbox-plugin/testing/seed_example_network.py), the result must equal the example network.
"""

import os

import pytest

from app.intent.builder import build_network
from app.services.netbox import fetch_intent_snapshot
from tests.fixtures.example_network import example_network

pytestmark = pytest.mark.integration

URL = os.environ.get("NETBOX_URL", "")


@pytest.mark.skipif(URL.endswith(".invalid") or not URL, reason="NETBOX_URL doesn't point at a live NetBox")
async def test_live_netbox_builds_a_valid_network() -> None:
    network = build_network(await fetch_intent_snapshot(URL, os.environ["NETBOX_TOKEN"]))
    if os.environ.get("NETBOX_SEEDED_EXAMPLE") == "1":
        assert network == example_network()
