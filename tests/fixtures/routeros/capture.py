"""Capture a RouterOS device's REST view of a rendered core, for the diff tests.

The lab is a CHR 7.25.1 (arm64) under QEMU in the spike's container (spikes/routeros/lab.sh), set up by hand:

    docker run -d --name sw-chr --privileged -p 127.0.0.1:8443:8443 -v $PWD/spikes/routeros:/spike sw-chr-lab sleep infinity
    docker cp chr-7.25.1-arm64.img sw-chr:/lab/      # from download.mikrotik.com
    docker exec sw-chr sh /spike/lab.sh net
    docker exec -d -e EXTRA_NIC=1 sw-chr sh /spike/lab.sh boot core tap0 tap1 1
    docker exec sw-chr sh /spike/bootstrap.sh /lab/core.console 192.168.88.1
    # then on the console: a `sharpwire` group (read,write,api,rest-api) and user, and a certificate signed by the
    # lab CA with subject-alt-name=IP:192.168.88.1,IP:127.0.0.1 for www-ssl; export the CA for ROUTEROS_LAB_CA
    docker exec -d sw-chr socat TCP-LISTEN:8443,fork,reuseaddr TCP:192.168.88.1:443

The CHR is only reachable from inside the container (or through that forward), so the capture runs in two halves:

    # on the host: render core-a, with its ports mapped onto the CHR's NICs
    uv run python -m tests.fixtures.routeros.capture render core-a > /tmp/core-a.resources.json
    docker cp /tmp/core-a.resources.json sw-chr:/lab/ && docker cp tests/fixtures/routeros/capture.py sw-chr:/lab/

    # in the lab container (python3 + httpx): remove sharpwire's rows, apply the render, read every core menu
    docker exec -e ROUTEROS_LAB_USER=sharpwire -e ROUTEROS_LAB_PASSWORD=... sw-chr \\
        python3 /lab/capture.py apply /lab/core-a.resources.json /lab/core-a.live.json

    # on the host: map the ports back and store the fixture
    docker cp sw-chr:/lab/core-a.live.json /tmp/ && \\
        uv run python -m tests.fixtures.routeros.capture store core-a /tmp/core-a.live.json

Re-capture after a render change, rather than editing the JSON by hand. The apply here is a lab shortcut (PUT and
PATCH in render order), not sharpwire's apply.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
# The CHR has virtio NICs: ether1 is the lab's mgmt, ether2 and ether3 stand in for the core's two SFP+ ports.
PORTS = {"sfp-sfpplus1": "ether2", "sfp-sfpplus2": "ether3"}
LAB_HOST = "192.168.88.1"


def _swap(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"[\w-]+", lambda m: mapping.get(m.group(0), m.group(0)), value)
    if isinstance(value, list):
        return [_swap(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _swap(v, mapping) for k, v in value.items()}
    return value


def render(device: str) -> None:
    from app.drivers.routeros.diff import MENUS
    from app.drivers.routeros.plan import CORE_MENUS
    from app.drivers.routeros.render import render_core
    from tests.fixtures.example_network import example_network

    network = example_network()
    resources = [
        {"path": r.path, "kind": r.kind, "key": list(r.key_fields), "fields": dict(r.fields)}
        for r in render_core(network, network.core_by_name[device])
    ]
    menus = [p for p in MENUS if p in CORE_MENUS]
    json.dump(_swap({"menus": menus, "resources": resources}, PORTS), sys.stdout, indent=1)


def apply(resources_file: str, out_file: str) -> None:
    import os

    import httpx

    spec = json.loads(Path(resources_file).read_text())
    c = httpx.Client(
        base_url=f"https://{LAB_HOST}/rest",
        auth=(os.environ["ROUTEROS_LAB_USER"], os.environ["ROUTEROS_LAB_PASSWORD"]),
        # The lab's own self-signed certificate, on the container's private bridge.
        verify=False,
        timeout=60,
    )

    def check(r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise SystemExit(f"{r.request.method} {r.request.url}: {r.status_code} {r.text}")
        return r

    for path in reversed(spec["menus"]):
        for row in c.get(f"/{path}").json():
            if row.get("comment", "").startswith("sharpwire") and row.get("dynamic") != "true":
                check(c.delete(f"/{path}/{row['.id']}"))
    for r in spec["resources"]:
        fields = r["fields"]
        if r["kind"] == "item":
            check(c.put(f"/{r['path']}", json=fields))
        elif r["kind"] == "baseline":
            rows = c.get(f"/{r['path']}").json()
            row = next(x for x in rows if all(x.get(k) == fields[k] for k in r["key"]))
            check(c.patch(f"/{r['path']}/{row['.id']}", json={k: v for k, v in fields.items() if k not in r["key"]}))
        else:
            check(c.post(f"/{r['path']}/set", json=fields))
    version = c.get("/system/resource").json()["version"]
    live = {path: c.get(f"/{path}").json() for path in spec["menus"]}
    Path(out_file).write_text(json.dumps({"version": version, "live": live}, indent=1))


def store(device: str, live_file: str) -> None:
    back = {v: k for k, v in PORTS.items()}
    captured = _swap(json.loads(Path(live_file).read_text()), back)
    (HERE / f"{device}.live.json").write_text(json.dumps(captured, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["render", device]:
            render(device)
        case ["apply", resources_file, out_file]:
            apply(resources_file, out_file)
        case ["store", device, live_file]:
            store(device, live_file)
        case _:
            raise SystemExit(__doc__)
