"""Capture a NetBox GraphQL snapshot for the builder tests.

    NETBOX_URL=http://127.0.0.1:8765 NETBOX_TOKEN=nbt_... uv run python -m tests.fixtures.netbox.capture example_network

Run it against the dev NetBox seeded by netbox-plugin/testing/seed_example_network.py (see the plugin README).
"""

import asyncio
import json
import os
import sys

from app.services.netbox import fetch_intent_snapshot
from tests.fixtures.netbox import HERE


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "example_network"
    data = asyncio.run(fetch_intent_snapshot(os.environ["NETBOX_URL"], os.environ["NETBOX_TOKEN"]))
    path = HERE / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
