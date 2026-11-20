# 7. Security and observability

Nov 20, 2026 · Draft

## Users

v1 has a minimal server-rendered web UI: the plan list, the diff view, approve or reject, apply, drift acknowledgement, and the audit log. It authenticates via Kanidm OAuth2/OIDC with PKCE, ported from sharpstack's `app/services/oidc.py`. That module already handles ES256 (Kanidm's default) by choosing the algorithm from the JWK `kty`, and it pins the discovery endpoints. sharpwire adds mapping from Kanidm groups to roles through a groups claim.

| Role | Can |
| --- | --- |
| viewer | Read plans, drift and the audit log |
| approver | Approve or reject plans (later: change sets), acknowledge drift and alerts |
| admin | Override protected objects; later: manage change classes, freezes and credentials |

## Credential storage

**v1:** target credentials are injected at deploy time as secret files, either systemd credentials or a mounted secret readable only by the controller. They are never stored in Postgres and never returned by the API. Rotating a credential means updating the secret and restarting the controller.

**(later)** Target credentials live in the controller's Postgres with envelope encryption: a data key per credential, wrapped by a master key kept outside the database and injected at deploy time. A database dump holds only ciphertext. The API can set and rotate credentials but never returns them.

## Target accounts

Each target has a dedicated sharpwire account with only what the driver needs:

| Target | Account | Rights |
| --- | --- | --- |
| RouterOS | Local user in a group with `read`, `write`, `policy`, `test`, `api`, `rest-api` (plus `sensitive` if needed). `policy` is close to admin | `read`, `write`, `api`, `rest-api`, plus `policy` and the scheduler and script rights the armed rollback depends on. No `ssh`, `winbox` or `ftp` beyond what is needed |
| PVE API | API token for a `sharpwire@pve` user | `SDN.Allocate` and `SDN.Audit` on `/sdn`, `Sys.Audit` on `/` (cluster firewall options), and `VM.Audit` on `/vms`, which is read-only and used for the isolation cross-check of guest NICs and firewall options. No VM write, storage or user rights. sharpstack's own token loses its `SDN.*` rights, apart from `SDN.Use` |
| Proxmox node | `sharpwire` Unix user with an SSH key | Sudo restricted to the `sharpwire-node` helper, which only touches the managed files, the rollback marker and timer, and the listed reload commands. This is **root-equivalent** all the same: the helper writes `/etc/network/interfaces`, whose `post-up` lines run as root. The restriction limits accidents, not a stolen key; protect the key accordingly |
| VyOS **(phase 2)** | HTTP API key | Full config. VyOS has no finer API scoping, so the key is limited to the mgmt network |

API and SSH reach targets only from the controller's address on the mgmt network. Each target's firewall should allow just that.

## Observability and alerting

### v1: alerts in Postgres, shown in the web UI

In v1 there is no Alertmanager and no chat. The controller writes alerts to an `alerts` table in its Postgres, and the web UI shows them.

- **Fields.** Each alert has a severity (`critical` or `warning`), a kind, the target, the related plan or run if any, a message, and timestamps for raised, acknowledged and resolved.
- **Deduplication.** Each alert has a dedup key and a topic, following sharpstack's outbox pattern. The same condition on the same target updates the open alert instead of creating a new one.
- **Auto-resolve.** An alert resolves itself when the condition clears, for example when drift goes away or a reconcile succeeds.
- **Acknowledge.** Approvers can acknowledge alerts, and the acknowledgement is recorded in the audit log.
- **UI.** A banner with counts of open critical and warning alerts on every page, plus an alert list filtered by target and state.

| Event | Severity |
| --- | --- |
| Rollout failed, rollback fired, run interrupted | critical |
| Corosync lost quorum or a node restored at boot during a run | critical |
| Reconcile stale for a target | critical |
| Drift detected, including foreign pending SDN changes | warning |
| Cross-target consistency check failing on live state | warning |
| Public-zone isolation off: cluster firewall disabled, or a public NIC or VM without the firewall or ipfilter | critical |
| Protected-object override | warning |

**Known v1 gap.** Nothing pages anyone, and the controller can't report its own outage. Alerts are only seen when someone opens the UI, and a dead controller raises nothing. That's acceptable while every change is driven by a person in the UI anyway. Paging is the first thing to add once sharpwire runs unattended.

### Later

- **Alertmanager.** The alert table maps directly onto it: critical alerts page, warnings don't. "Controller down" comes from Prometheus scraping the controller.
- **Chat.** The informational stream, so chat never becomes a second pager: drift, consistency failures and overrides, plus change set created, awaiting approval, auto-applied, and freeze started or ended, each with a link.
- **Prometheus metrics:**
  - drift per target;
  - change sets and runs by outcome and change class;
  - time since the last successful reconcile per target;
  - queue depth.

## Audit log

The audit log mirrors sharpstack's `audit_logs`: rows are written at the API layer with IP, user agent and session, and `snapshot(secret_fields=)` redacts secrets. It records every plan, change set, approval, override, apply and rollback, with pre- and post-apply snapshots per target.
