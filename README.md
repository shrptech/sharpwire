# sharpwire

sharpwire keeps MikroTik and Proxmox VE (and, later, VyOS) matching the intent in NetBox. It renders declarative config, detects drift, and applies human-approved plans with a rollback armed on the device.

- Design: [`docs/README.md`](docs/README.md)
- Working in this repo: [`AGENTS.md`](AGENTS.md)

## Quick start

```sh
docker compose up -d postgres
cp .env.example .env
uv sync
uv run pytest
uv run fastapi dev app/main.py
```
