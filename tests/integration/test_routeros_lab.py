"""The RouterOS client and diff against the CHR lab (spikes/routeros/), with core-a applied.

Start the lab and capture core-a as tests/fixtures/routeros/capture.py describes, publish the CHR's REST port to the
host (the lab container forwards 127.0.0.1:8443 to it), then run:

    ROUTEROS_LAB_URL=https://127.0.0.1:8443/rest ROUTEROS_LAB_CA=<the lab CA's PEM> \\
    ROUTEROS_LAB_USER=sharpwire ROUTEROS_LAB_PASSWORD=... uv run pytest -m integration tests/integration
"""

import os

import pytest

from app.drivers.routeros.client import RouterOSClient, tls_context
from app.drivers.routeros.plan import CORE_MENUS, plan_core
from tests.fixtures.example_network import example_network
from tests.fixtures.routeros.capture import PORTS, _swap

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("ROUTEROS_LAB_URL"), reason="ROUTEROS_LAB_URL doesn't point at the CHR lab"),
]


async def test_the_lab_core_reads_back_in_sync() -> None:
    async with RouterOSClient(
        os.environ["ROUTEROS_LAB_URL"],
        os.environ["ROUTEROS_LAB_USER"],
        os.environ["ROUTEROS_LAB_PASSWORD"],
        tls_context(os.environ["ROUTEROS_LAB_CA"]),
    ) as client:
        live = _swap(dict(await client.read(CORE_MENUS)), {v: k for k, v in PORTS.items()})

    network = example_network()
    draft = plan_core(network, network.core_by_name["core-a"], live)
    # Only what a CHR can't do: l2mtu on virtio ports.
    assert {(op.path, tuple(op.fields)) for op in draft.ops} == {("interface/ethernet", ("l2mtu",))}
