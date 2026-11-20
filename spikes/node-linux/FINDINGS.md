# Spike: Proxmox node host files on Debian 13 (no PVE)

Nov 20, 2026. Lima VM `sharpwire-lab`: Debian 13 arm64, kernel 6.12 cloud. ifupdown2 `3.3.0-1+pmx14` (Proxmox fork, arch `all`, installs fine on arm64). FRR 10.7.1 (deb.frrouting.org `frr-10`; PVE 9 ships 10.6.1). PVE's real merge `PVE::Network::SDN::Frr::append_local_config()` (libpve-network-perl 1.6.10, git HEAD) was run with stub modules and rendered with Proxmox's own templates (`proxmox-ve-rs/proxmox-frr-templates`) through jinja2 (`pve-merge/`).

Emulated NICs: `eno1`/`eno2` are veths into netns `leaf`, where an 802.3ad bond acts as the partner. **LACP negotiated** (both ports in aggregator 1), so 802.3ad was used, not balance-xor. The leaf also carries the VLAN 10/101/102 gateways, and an FRR instance there plays core A (`frr@leaf`, `--netns`).

## Summary

| Q | Verdict | Consequence |
| --- | --- | --- |
| 1 Dry run and drift | partly | `ifreload -a -n` makes no changes but exits 0 on invalid files. Validate with `ifreload -a -s`. `ifquery --check` misses missing routes, extra interfaces and removed stanzas |
| 2 VRF ordering | confirmed, worse | Missing VRF: exit 1, but `bond0.300` still comes up in the default VRF. The helper must refuse first |
| 3 Rollback | confirmed | Timer, disarm, reboot and hard power-off all restore. The restore must re-run sysctl |
| 4 `frr.conf.local` on FRR 10.x and the PVE merge | refuted | `neighbor … deactivate` is invalid, the `bfd` block is dropped, and the duplicate router-id and `advertise` lines oscillate |
| 5 Per-node reload | confirmed, conditional | `frr-reload.py` keeps sessions up only if the file reloads the same way every time. A restart always drops them |
| 6 Post-merge check | works, brittle | Catches the bad states. Use JSON where it exists |

## Evidence

### Q1. ifupdown2 (`results-q1-*.txt`, `etc/interfaces.pve1`)

- The Step 2 file applies. The mgmt default `via 2001:db8:100::1 dev bond0.10` comes up and pinging the gateway works.
- **The dry run is not a validator.**
  - `ifreload -a -n` exits 0 for `address notanaddress`, an unknown keyword, and `vrf <missing>`.
  - The pmx fork also prints dry-run bugs that aren't real errors: `addr_add_dry_run() got an unexpected keyword argument 'nodad'`, `link_add_vlan_dry_run() takes …`.
  - `ifreload -a -s` (syntax check) exits 1 on invalid values. Unknown keywords still pass with only a warning.
  - On SDN's own `interfaces.d/sdn` it warns (`bridge-fd 0` out of range), so the helper must filter that.
- **`ifquery --check -a`:** exit 0 when clean, 1 on drift.
  - Detects: an out-of-band address add or delete (including mgmt), an out-of-band MTU change, a changed address in the file, a stanza added to the file.
  - **Misses** (exit 0): a deleted default route (the gateway isn't checked); an interface created by hand that isn't in the file; a stanza removed from the file while the interface still runs. `ifreload -a` would then ifdown it.
- Pitfalls:
  - Without `/run/network`, every non-query command fails with the misleading `Another instance of this program is already running` (exit 89). The helper must `mkdir -p /run/network`.
  - Removing a stanza ifdowns that interface. In the lab, deleting cloud-init's `interfaces.d` file downed the VM's `eth0`.

### Q2. VRF ordering (`results-q2-vrf.txt`)

- With no `vrf_public`: `ifreload -a -s` and `-n` exit 0 with warnings. `ifreload -a` prints `error: bond0.300: vrf master ifaceobj vrf_public not found` and **exits 1, but still creates `bond0.300` with both addresses in the main table**. It doesn't create the VRF. `ifquery --check` then shows `vrf None [fail]`.
- After installing an emulated `interfaces.d/sdn` (`vrf_public` with `vrf-table auto`, plus vxlan and bridge) and running `ifreload -a`: exit 0, `bond0.300` is `master vrf_public`, the routes move to the VRF table, and the check is clean.
- `modprobe vrf` works.

### Q3. Rollback (`scripts/sharpwire-node`, `etc/sharpwire-restore.service`, `results-q3*.txt`)

- **Timer:** `systemd-run --collect --on-active=30s --timer-property=AccuracySec=1s --unit sharpwire-rollback-<id> …` fired, restored the mgmt address and removed the marker. Disarming (stop the `.timer`, then remove the marker) prevented the restore.
- **Unit ordering that works on trixie:**
  - `DefaultDependencies=no`
  - `RequiresMountsFor=/var/lib/sharpwire`
  - `After=local-fs.target systemd-sysctl.service`
  - `Before=network-pre.target networking.service frr.service pve-cluster.service corosync.service`
  - `ConditionPathExists=/var/lib/sharpwire/armed`
  - `WantedBy=sysinit.target`
- **Boot run:** a broken config (mgmt address removed, forwarding=0, corrupted `frr.conf`) was armed, then the VM rebooted. Sequence: `systemd-sysctl` (0.70 s) → restore (1.87 s) → networking (1.92 s) → frr (4.05 s). Mgmt address, forwarding=1 and `frr.conf` all came back. No ordering cycles. Hard power-off (`limactl stop -f`) gave the same result.
- **Pitfalls:**
  - `networking.service` and `frr.service` are both `After=systemd-sysctl.service`, so the broken sysctl is applied before the restore runs. The restore must re-run `systemd-sysctl` itself.
  - Write the marker atomically and `sync` it.
  - The restore removes the marker, so the snapshot isn't re-applied on later boots.
  - `--collect` discards the unit's result, so record outcomes in the journal.
  - frr.service has `StartLimitBurst=3` per 3 min, so repeated restarts fail.

### Q4. FRR config (`frr/`, `pve-merge/`, `results-q4-*.txt`)

- `vtysh -C -f` accepts block 4a. It rejects 5c with **`% Unknown command: neighbor CORE6 deactivate`**; the correct form is `no neighbor CORE6 activate`.
- FRR 10.x accepts `local-as … no-prepend replace-as` on a peer-group, `bfd profile` on a peer-group, and `set ipv6 next-hop prefer-global`. The core sees AS 4200000011. `no neighbor UL activate` in ipv4 disappears, because the generated `no bgp default ipv4-unicast` makes it redundant.
- **PVE's merge** (shipped code, `merge.stderr`):
  - **drops the top-level `bfd` block**. `neighbor … bfd profile X` is still accepted, so FRR silently runs default timers;
  - renumbers route-map entries 1..n (NO-DEFAULT 10/20/100 → 1/2/3);
  - appends local `router bgp` lines after the generated ones, giving two `bgp router-id` lines;
  - keeps the generated `advertise ipv4|ipv6 unicast` and adds our `… route-map NO-DEFAULT` after it;
  - duplicates `redistribute connected`.
- The EVPN plugin with a peers-only controller generates **no `update-source`**, so ours is needed. It generates the `router-id` from bytes 2–5 of the MAC of the interface holding the local peer IP (`dummy0`). The `vrf_public` router gets that MAC-derived id too.
- **`frr-reload.py --reload` (what SDN apply runs) oscillates** (`results-q4-reload-merged.txt`). After a restart: router-id 10.255.0.11, NO-DEFAULT present. After reload 1: router-id 203.0.113.17, plain `advertise` lines. After reload 2: back to the first state. Each flip resets every BGP session.
- **Fix (v2/v3, stable across 3 reloads):**
  - remove `bgp router-id` and set `hwaddress 02:00:0a:ff:00:0b` on `dummy0` (ifupdown2 applies it and `ifquery --check` passes; derived router-id 10.255.0.11);
  - set `advertise-subnets 0` on zone `public` and keep our `redistribute` and `advertise … route-map NO-DEFAULT` lines;
  - put the BFD profiles inside an `interface dummy0` block, which the merge keeps verbatim. vtysh falls back to the parent node, so the profiles are created (VTEP peers show `Profile: overlay`, `Detect-multiplier: 5`).
- **Inline per-neighbour timers** (`neighbor UL bfd 3 300 300`) are accepted and effective, but `show run` prints only `neighbor UL bfd`. So frr-reload deletes and re-adds BFD on every reload, which flaps sessions (Q5). Don't use them.
- **Harmless frr-reload noise:** `hostname`, the `frr version` line, the redundant `no neighbor UL activate` and `neighbor CORE activate`, and on 10.7.1 `use-underlays-nexthop-weight` and `bgp deterministic-med` (the last flips on and off with reloads; recheck on 10.6.1).

### Q5. Per-node reload (`scripts/q5-reload-test.sh`, `q5b-variants.sh`, `results-q5*.txt`)

eBGP v6 plus BFD between pve1 and core A over `bond0.101`. Counters read on the core.

| Case | Session |
| --- | --- |
| Profiles or plain `bfd`: no-op reload | Stays up |
| Profiles or plain `bfd`: unrelated route-map change | Stays up |
| Inline timers on UL: every reload, including a no-op | Drops (`dropped` +1 each time) |
| Router-id flip | Drops |
| `systemctl restart frr` | Idle, BFD down, back in about 10 s |

Changing UL-OUT, which the session uses, also didn't reset it (variants A and B).

### Q6. Post-merge check (`sharpwire-node postmerge-check`, `results-q6-postmerge.txt`)

- Passes on v3. On the doc's config it fails `bfd profile` right after a restart; after one reload it also fails router-id and both NO-DEFAULT lines.
- Robustness:
  - it relies on `show running-config` text and exact indentation;
  - profile values equal to the defaults aren't printed (`profile underlay` ends up empty);
  - `bgp router-id` appears in each router block.
- Better sources: `show bgp vrf all summary json` (router-id per VRF), `show bfd peers json` (profile, timers), and `show bgp vrf vrf_public l2vpn evpn …` or text for the `advertise` route-map (no JSON found).

### Q7. Contradictions with the docs

- Network doc 5c: `neighbor CORE6 deactivate` is invalid.
- Network doc 4a: "your router-id has replaced the generated one" isn't stable under frr-reload.
- Network doc 5c: "merge NO-DEFAULT with whatever SDN generates" doesn't work; the generated line wins every other reload.
- Network doc 4a/4c: the BFD profiles never reach FRR.
- 04: "`ifreload -a -n` as a dry run" validates nothing.
- 05: `ifquery --check -a` alone isn't enough for the drift precondition.
- 04: the per-node mechanism is PVE's own: `/usr/lib/frr/frr-reload.py --stdout --reload /etc/frr/frr.conf`, falling back to `systemctl restart frr` (`Frr.pm::apply`).

## Recommended doc changes

- **Network doc 4a/4c/5c and Step 2:**
  - drop `bgp router-id`; add `hwaddress` on `dummy0` encoding the router-id (`02:00:<4 router-id bytes>`);
  - move the BFD profiles into an `interface dummy0` wrapper (or accept FRR defaults and drop the overlay ×5);
  - `no neighbor CORE6 activate`;
  - `advertise-subnets 0` on zone `public`.
- **04, "Node targets: host files":**
  - validate with `ifreload -a -s`; `-n` is a preview only;
  - FRR mechanism: `frr-reload.py --reload`, after `--test`;
  - the helper checks `ip link show vrf_public type vrf` exists before writing `bond0.300`, because the failure is partial, not atomic;
  - post-merge check: JSON for router-id and BFD, add "BFD profiles present" and "stable across two reloads", never compare route-map sequence numbers;
  - `frr.conf.local` must not duplicate generated keys.
- **05, "Proxmox nodes (host files)":** the unit ordering above; the restore re-runs sysctl and clears the marker; write the marker atomically and sync it; create `/run/network`.
- **05, "Proxmox cluster: SDN":**
  - drift precondition = `ifquery --check -a` + a default-route check + "no running interfaces outside the file and interfaces.d";
  - `frr-reload --test` never returns an empty diff, so FRR drift needs a normalized comparison;
  - an SDN apply that reloads an unstable `frr.conf` resets sessions cluster-wide.
- **08, Testing:** golden tests of PVE's merge (`append_local_config` + templates, as in `pve-merge/`) and a CI check that two consecutive frr-reloads keep sessions. Risks: the `frr.conf.local` merge row should cover dropped blocks and reload oscillation.

## What this emulation can't prove

pve-manager's network reload and `interfaces.new` promotion; the real SDN apply API, locks and rollback; pmxcfs and corosync, quorum and HA fencing timing; PVE's Rust renderer (approximated with jinja2) and FRR 10.6.1 (we ran 10.7.1); real NIC/LACP with switches and real BFD timing; `/var` on a separate filesystem at boot; the EVPN data plane.

The lab changed the VM itself: cloud-init started writing an `eth0` ENI file once ifupdown2 was installed, and the fake IPv6 default route hijacked the VM's own IPv6 egress.

## Files

`spikes/node-linux/`:
- `scripts/sharpwire-node` — helper prototype: snapshot, arm, disarm, restore, boot-restore, validate, drift, frr-reload, postmerge-check
- `etc/sharpwire-restore.service`
- `scripts/lab-nics.sh`, `etc/lab-nics.service`
- `etc/interfaces.pve1`, `etc/interfaces.d-sdn`
- `frr/frr.conf.local.pve1.v3` — the recommended `frr.conf.local`
- `pve-merge/merge.pl`, `pve-merge/render.py` — PVE merge emulation
- `pve-merge/frr.conf.merged.pve1*` — rendered results
- `results-q*.txt` — raw command output per question
