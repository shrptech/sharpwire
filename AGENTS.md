# AGENTS.md

Guidance for anyone, human or agent, working in this repository.

## What this is

sharpwire keeps the network matching the intent in NetBox: MikroTik cores and leafs, Proxmox VE (SDN, node files, cluster files) and, in phase 2, the VyOS gateways. It renders full declarative config, diffs it against live state, and applies human-approved plans with a rollback armed on the target.

**The design is in [`docs/`](docs/README.md). Read it before changing behaviour.** Start with the README's "v1 in one table" and "Decisions". Anything in [`docs/later.md`](docs/later.md) is designed but must not be built yet. If code and the docs disagree, fix whichever is wrong in the same change. The network design ([`docs/network/`](docs/network/proxmox-evpn-ipv6-underlay.md)) wins over everything else.

sharpwire is a sibling of sharpstack (`../sharpstack`) and follows its conventions unless this file says otherwise. When in doubt, read sharpstack's `AGENTS.md` for the rule and copy it.

## Commands

```sh
docker compose up -d postgres         # dev Postgres on 127.0.0.1:5433
cp .env.example .env                  # then fill in NetBox
uv sync
uv run fastapi dev app/main.py        # controller with reload
uv run alembic upgrade head
uv run pytest                         # unit tests (tests/integration/ needs the labs)
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pre-commit install             # once per clone
```

## Layout

| Path | What goes there |
| --- | --- |
| `app/api/` | FastAPI routers, plus underscore-prefixed helpers used by two or more route modules |
| `app/models/` | SQLAlchemy tables. Import every module in `app/models/__init__.py` |
| `app/schemas/` | Pydantic wire shapes |
| `app/services/` | Everything else that isn't a router, a job or a driver. Imports neither `fastapi.Depends`/`Request` nor `app.api` |
| `app/jobs/` | The Postgres job queue (`SELECT … FOR UPDATE SKIP LOCKED`) and job bodies. No Celery, no Redis |
| `app/intent/` | The vendor-neutral intent model (Pydantic) that drivers render from, with the cross-target rules |
| `app/drivers/` | One package per target type (`routeros`, `proxmox`, later `vyos`): render, fetch live state, apply, health checks |
| `app/enums.py` | Every shared value list, as a `Literal`. Imports nothing from the app |
| `alembic/` | Migrations |
| `tests/` | Unit tests; `tests/integration/` needs live NetBox and the labs |

Still to come, per the build plan: the NetBox plugin (its own package, built against NetBox 4.7) and the Proxmox node bootstrap package (the `sharpwire-node` helper, its sudoers rule and `sharpwire-restore.service`).

Import direction: `api → jobs, services, drivers, schemas, models`; `jobs → services, drivers, schemas, models`; `drivers → intent, schemas`, never `api` or `jobs`. `intent` imports nothing from the app.

## Rules specific to sharpwire

- **NetBox holds the full state.** Every value that reaches a device comes from the intent model (built from NetBox) or a named rule listed in [`docs/02-intent-model.md`](docs/02-intent-model.md#rules). No defaults for network values in code: a new constant is a new rule in that table, added in the same change.
- **Never touch anything outside a driver's managed scope.** Each driver defines what it reads, diffs and may change ([`docs/04-drivers.md`](docs/04-drivers.md)). Anything outside it is never read for the diff and never written.
- **Every write goes through a plan.** No code path changes a target without an approved, non-stale plan, the target's lock, an armed rollback (where the docs require one) and health checks.
- **The hard guards have no override** ([`docs/03-pipeline-and-validation.md`](docs/03-pipeline-and-validation.md#hard-guards)). Never add one.
- **RouterOS objects are keyed by natural keys, never `.id`.**
- **sharpwire is the only writer of Proxmox SDN.** Don't add PVE calls outside `app/drivers/proxmox/`, and never call `/cluster/sdn` from anywhere else.
- **Credentials** come from settings only: environment variables in dev, secret files in production (`app/config.py`). Never log them, store them in Postgres (v1), or return them from the API.
- **The per-target apply lock** is a session-level `pg_advisory_lock` on the apply worker's own connection, not sharpstack's `advisory_xact_lock`: an apply lasts minutes.

## Conventions inherited from sharpstack

- **Stack:** Python 3.14, FastAPI, async SQLAlchemy 2 with asyncpg, Alembic, pydantic-settings, uv, ruff (line length 120) and mypy (`disallow_untyped_defs`).
- **Config:** every setting comes from `app/config.py`. The app refuses to start unless every setting without a default is present.
- **Errors:** every error response is `{"detail": ..., "error_code": ... | null}`. Raise `AppHTTPException(..., error_code=...)` for failures worth branching on. `detail` is shown to users verbatim, so write what to do, in plain short sentences. Log the underlying exception (`logger.warning(..., exc_info=True)`) instead of interpolating it.
- **Naming:**
  - tables are plural snake_case;
  - model classes are singular CapWords, with acronyms fully capitalized;
  - constraints and indexes are named `ck_<singular table>_<column>`, `uq_<plural table>_<columns>` and `ix_<plural table>_<columns>`;
  - every NOT NULL column with a Python `default=` also declares `server_default=`;
  - Pydantic schemas are `<Model>Create`/`Update`/`Response`/`Page`;
  - booleans use `is_` only for identity predicates; toggles are positive bare words (`managed`, not `is_unmanaged`).
- **Migrations:** every schema change gets its own new revision, never folded into an old one.
- **Audit log:** mirror sharpstack's `audit_logs`. Rows are written at the API layer with IP, user agent and session, and secrets are redacted with a `snapshot(secret_fields=)` helper.
- **Alerts:** a dedup key and a topic with supersede and withdraw semantics, as in sharpstack's notification outbox.

## Deliberate differences from sharpstack

- **Job queue:** Postgres instead of Celery and Redis. There is one controller with low job volume, and the apply gate lives with the plans.
- **UI:** a minimal server-rendered operator UI (Jinja templates via `fastapi[standard]`), not a SPA.
- **Staff login:** Kanidm OIDC (ported from sharpstack's `app/services/oidc.py`, plus groups → roles), not passkeys.

## Testing

Unit tests need no network. Tests that touch the database use the real Postgres from `docker compose`, never a mocked DB layer, as in sharpstack. Driver tests use golden files rendered from NetBox fixtures; the network doc's examples are the first golden files. `tests/integration/` runs against live NetBox and the containerlab or nested Proxmox lab, and is marked `integration`.
