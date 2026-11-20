# Spike: RouterOS 7 REST, rollback and BGP on CHR

Nov 20, 2026. **RouterOS 7.25.1** (newest stable; long-term is 7.24.6), CHR arm64 under QEMU TCG inside Docker on colima. Scripts in this directory. Image: scratchpad `chr/chr-7.25.1-arm64.img`; Docker image `sw-chr-lab` (`docker rmi sw-chr-lab` to remove).

## Summary

| Q | Verdict | Consequence |
| --- | --- | --- |
| 1 REST basics | confirmed, with caveats | See REST below |
| 2 Policies | **refuted** | Minimal set is `read,write,api,rest-api`. `policy` and `test` aren't needed |
| 3 Armed rollback | confirmed | Fires, tolerates a failed op, disarms, survives a reboot, with one gap (missed one-shots). `/export` works over REST |
| 4 BGP | confirmed | `/routing/bgp/instance` exists. A prefix listener accepts several peers with the same AS. All property names in the docs are valid |
| 5 Filter rules | partly | Order is exposed via `.nextid`. `comment` isn't unique |
| 6 Third-party next hop | **`default` refuted, `propagate` confirmed** | Use `nexthop-choice=propagate` on node-facing connections |
| 7 Doc contradictions | several | BFD gates BGP, link-local gateways, missed schedules, no `admin` group |

## Q1. REST

- Bootstrap after `no-defaults`: self-signed CA and cert via `/certificate sign`, then `www-ssl certificate=https`. CHR still has `/ip dhcp-client add interface=ether1` after `no-defaults`; 7.25 also adds `reverse-proxy` entries in `/ip service`.
- Methods: PUT creates (201 with the full object), PATCH sets (200), DELETE removes (204), POST runs commands.
- Errors: `{"detail":"failure: already have interface with name br-test","error":400,"message":"Bad Request"}`; an unknown property gives `unknown parameter bogus`; a bad password gives `401`.
- Filtered reads: `GET ?name=x&.proplist=a,b` for equality; otherwise `POST /rest/interface/print {".proplist":[…],".query":["type=bridge"]}`.
- **All values are strings. Properties at their default are omitted from GET**, so the diff must treat a missing field as the default.
- Static `.id`s were stable across a reboot; dynamic ones (link-local, `::1`) changed. Deleted `.id`s aren't reused. `PATCH /rest/<path>/<name>` works for named objects, but on any error it reports "missing or invalid resource identifier". Resolve natural key → `.id` with a GET on every request.
- **Deleting an interface leaves its addresses orphaned** (`interface=*4`), so remove dependents first.

## Q2. Policies

12 combinations tested. Without `api`: `500 std failure: not allowed (9)` and "login failure via api" in the log. Without `rest-api`: 401. **`read,write,api,rest-api`** was enough for all the core config, `/ip service`, creating scripts and schedulers, the scheduled runs and the export.

- A script or scheduler takes its creator's policies (`owner=sharpwire policy=read,write`).
- `sensitive` only unmasks secrets such as `tcp-md5-key` (shown as `*****` without it, also in `export show-sensitive`).
- Not available to this account: `/system/reboot` and `/export file=`.
- The built-in groups are `read`, `write` and `full`; there is no `admin` group.

## Q3. Armed rollback

- **Fire:** a script uploaded via REST plus a one-shot scheduler reverted a BGP connection, a filter rule and a changed address, then removed its own scheduler.
- **Failed op:** with each op wrapped in `:do { } on-error={ }`, the first op failed ("op1 … failed" in the log) and ops 2 and 3 still ran.
- **Disarm:** deleting the scheduler before it was due left the changes in place.
- **Reboot:** the scheduler entry persisted and fired at its time after boot.
- **Gap:** a one-shot scheduler whose time passes while the router is down **never fires**. `run-count` resets at boot, so it can't show whether a rollback fired. A `start-time=startup` entry did fire at boot.
- **Recommendation:** arm two entries (T+N and `startup`). The rollback script removes both and leaves a durable marker (e.g. a comment). Disarm removes both.
- **Export:** `POST /rest/execute {"script":"/export","as-string":""}` returns the whole export in `ret`. A 69 KB, 1,286-line export came back complete. **`/execute` always returns HTTP 200, even on errors** (`ret: "bad parameter bogus (line 1 column 30)"`), so the driver must parse `ret`. The export compresses dotted names (`remote.address=… .as=…`).

## Q4. BGP

This PUT returned 201:

```
PUT /rest/routing/bgp/connection {"name":"pub6","instance":"core","afi":"ipv6","local.role":"ebgp","local.address":"2001:db8:250::1","remote.address":"2001:db8:250::/64","remote.as":"4200000100","use-bfd":"yes","input.filter":"pub-in","output.filter-chain":"default-out","output.default-originate":"always"}
```

- Two dynamic sessions came up, **both named `pub6-1`**, so session names aren't unique; key sessions by `remote.address`.
- `nexthop-choice` accepts `default|force-self|propagate`.
- `input.filter-chain` and `output.filter` are unknown parameters, so the docs' asymmetric names (`input.filter`, `output.filter-chain`) are right.
- The connection shows `"as"`, inherited from the instance.
- **With `use-bfd=yes`, 7.25 won't establish BGP until BFD is up** ("Cannot make BGP session. BFD session was not established"). A `/routing/bfd/configuration` covering the interface is required.

## Q5. Filter rules

Shape: `{".id":"*E",".nextid":"*12","chain":"pub-in","comment":"…","rule":"…"}`. `.nextid` links all rules in one table-wide list; per-chain order is that list filtered by chain. `place-before` works on PUT, and `POST …/move {numbers,destination}` reorders. Rule syntax is validated on create. Duplicate comments are accepted, so the driver must enforce uniqueness itself.

## Q6. Third-party next hop

With FRR peers on a shared subnet:
- `default`: peer B received A's /128 with the core as next hop (`2001:db8:101::1`).
- `propagate`: peer B received the global next hop `2001:db8:101::11` plus A's link-local, and the kernel route went via A's link-local directly.

The core itself installs node routes via `fe80::…%vlan101`, because FRR peers send link-local next hops.

## Golden check (`tests/golden/routeros/core-a.rsc`)

Note: this check ran on the golden as it was then, with `nexthop-choice=default` and no rule comments. The golden now uses `propagate` (Q6) and `sharpwire:` comments (Q5); both forms were exercised separately in this spike.

Applied line by line via `/execute` on a fresh no-defaults CHR, with sfp-sfpplus1/2 → ether2/ether3. **All 30 lines accepted**, including `lacp-rate=1sec`, `/routing bgp instance`, the BFD configuration, `nexthop-choice=default`, `input.filter`, `output.filter-chain`, `output.default-originate=always`.

Silent changes: `l2mtu=9500` accepted but not applied on virtio ethernet (likely a CHR/virtio limit; real SFP+ needs the hardware lab), so the export flags vlan101/vlan300 `# MTU > L2MTU`. The IPv6 `/64` is elided in the export.

Live JSON:

```
{"afi":"ipv6","as":"4200000001","input.filter":"ul-in-pve1","instance":"core","local.address":"2001:db8:101::1","local.role":"ebgp","name":"ul-pve1","nexthop-choice":"default","output.filter-chain":"ul-out","remote.address":"2001:db8:101::11","remote.as":"4200000011","routing-table":"main","use-bfd":"true"}
{"chain":"pub-in","rule":"if (dst in 198.51.100.0/24 && dst-len in 24-28) { accept }",".nextid":"*A"}
```

Note `use-bfd` reads back as `"true"` after being written as `"yes"`: the diff must normalize booleans.

## Recommended doc changes

- **04, "Rollback support":** policies `read,write,api,rest-api` (+`sensitive`); drop `policy`. Built-in groups are `read`, `write`, `full`.
- **04, object identity:** filter rules by chain + a sharpwire comment, order from `.nextid`, uniqueness enforced by the driver; BGP sessions by `remote.address`; resolve `.id` per request; delete in dependency order; missing field = default; normalize booleans.
- **05, "RouterOS" apply:** arm a T+N and a `startup` scheduler; the script disarms itself and leaves a durable marker; export via `/execute as-string`; check `ret` on every `/execute`; don't rely on `run-count`.
- **05, "Health checks" (core):** node routes have a link-local gateway on the VLAN interface; BGP stays down until BFD is up.
- **Network doc Step 4b / A.2:** `propagate` on node-facing connections; on the node expect a direct next hop (global or link-local) on `bond0.101`.
- **07, "Target accounts":** same policy correction.
- **08, risks:** BFD gating BGP; schedules missed during downtime; `/execute` returning 200 on errors.

## Not tested / limits

- BFD with an FRR peer on the active (non-listener) connections didn't come up in the time box; those ran with `use-bfd=no`. The listener side worked with BFD.
- `l2mtu`, LACP and VRRP need real hardware.
- colima only shares `$HOME`, so the image was copied in with `docker cp`; an empty `/private/tmp/claude-501/...` directory was left inside the colima VM.
- `--privileged` was used for the tap interfaces, bridges and network namespaces.

## Files

- Lab: `Dockerfile`, `lab.sh`, `console.py`, `bootstrap.sh`, `frr_peers.sh`
- REST client: `rest.py`
- Per question: `q2_policies.py`, `cleanup_q2.py`, `q3_rollback.py`, `ids_snapshot.py`, `q4_core.py`
- Golden check: `golden_apply.py`
