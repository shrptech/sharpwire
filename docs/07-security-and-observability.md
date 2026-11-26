# 7. Security and observability

Nov 20, 2026 · Draft

## Users

The web UI logs in through Kanidm OIDC with PKCE, using a port of sharpstack's `app/services/oidc.py`. That module already handles ES256, Kanidm's default, and pins the discovery endpoints. In v1 there is a single role: members of the Kanidm group `sharpwire`, read from the groups claim, can see everything and approve plans. Separate roles come [later](later.md#overrides-and-roles).

## Credentials

Target credentials are injected at deploy time as secret files (systemd credentials or a mounted secret). They're never stored in Postgres or returned by the API, and rotating one means replacing the file and restarting the controller (`app/config.py`). Credentials in Postgres come [later](later.md#alerting-and-credentials).

## Target accounts

| Target | Account | Rights |
| --- | --- | --- |
| RouterOS | Local user in a dedicated group | `read`, `write`, `api`, `rest-api`. Verified on RouterOS 7.25.1 to cover all config, the armed rollback and the export. No `policy`, `ssh`, `winbox`, `ftp` or `reboot` |
| PVE API | Token for `sharpwire@pve` | `SDN.Allocate` and `SDN.Audit` on `/sdn`, `Sys.Audit` on `/` (firewall options, per-node tasks, dry-run). No VM, storage or user rights |
| Proxmox node | `sharpwire` user with an SSH key | Sudo restricted to the `sharpwire-node` helper. This is still **root-equivalent**, because the helper writes `/etc/network/interfaces`, whose `post-up` lines run as root. Protect the key accordingly |

Targets accept API and SSH connections only from the controller's address on the mgmt network.

## Alerts

v1 stores alerts in an `alerts` table and shows them in the UI. Until the UI arrives in step 5, the read-only JSON API serves them (`GET /api/alerts`, with plans and targets), behind a bearer token (`API_TOKEN`, at least 16 characters, like the webhook's `NETBOX_WEBHOOK_SECRET`). The API serves no schema or docs pages without the token, so none at all. Each alert has:
- a severity, target, related plan or run, and message;
- a dedup key and topic, following sharpstack's outbox pattern, so a repeat updates the open alert;
- automatic resolution once the condition clears;
- an acknowledgement, recorded in the audit log.

| Event | Severity |
| --- | --- |
| Rollout failed, rollback fired, run interrupted | critical |
| Corosync lost quorum, or a node restored at boot during a run | critical |
| Reconcile overdue for a target | critical |
| Cluster firewall disabled | critical |
| A target differs from NetBox (including foreign pending SDN changes) | warning |
| A target can't be read or planned, or NetBox can't be read | warning |
| A cross-target check fails on live state | warning |

**Known gap:** nothing pages anyone, and a dead controller raises nothing. That's acceptable while every change is made by a person in the UI. Alertmanager is the first thing to add when that stops being true ([later](later.md#alerting-and-credentials)).

## Audit log

The audit log mirrors sharpstack's `audit_logs`: rows written at the API layer with IP, user agent and session, with secrets redacted. It records every plan, approval, apply and rollback, with pre- and post-apply snapshots.
