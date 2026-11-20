"""NetBox GraphQL snapshots: the `data` of app.services.netbox.INTENT_QUERY, captured from a real NetBox.

example_network.json is the network doc's example, seeded into NetBox 4.7.5 with the plugin by
netbox-plugin/testing/seed_example_network.py and captured with `python -m tests.fixtures.netbox.capture`.
"""

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((HERE / f"{name}.json").read_text())
    return data
