# 8. Testing, build plan and risks

Nov 20, 2026 · Draft

## Testing

A virtual lab gates every driver change in CI. A physical lab covers what virtual devices can't emulate.

**Unit and golden tests.**
- Renderer output against NetBox fixtures, including the network doc's examples as golden files (`frr.conf.local` for pve1, Appendix A).
- Diff ordering, change-set staging, and the change-class classifier.
- Every cross-target consistency rule, each with a failing fixture.

**Containerlab in CI.** Two CHRs act as cores and FRR containers act as Proxmox nodes running the rendered `frr.conf.local`. Every driver change must pass:
- **idempotency:** render → apply → re-read gives an empty diff;
- **rollback:** withhold the confirm and verify the target reverts;
- **topology:** underlay sessions up, third-party next hop (two next hops between nodes, never via `::1`), ECMP, the `vrf_public` handoff with only defaults received, and no /32 leaked to the cores;
- **phase 2:** VyOS containers as gateways, with EVPN up and type-5 defaults present in `vrf_cNN`.

**Nested Proxmox lab.** Three nested PVE VMs on the lab CHRs. This covers what FRR containers can't: the SDN API, SDN apply with the `frr.conf.local` merge, `ifreload`, the helper's armed rollback **including the boot-time restore after a forced reboot**, and corosync link changes. It runs nightly and before every release. sharpstack's integration suite also runs against this lab, on SDN applied by sharpwire.

**Physical lab.** Real CRS hardware for MLAG, bridge hardware offload and other switch-chip behavior CHR can't emulate. RouterOS and PVE upgrades are validated here before a driver's supported-version list is bumped.

## Build plan

Drift detection ships first, read-only, so the diff engine is proven before sharpwire can write to anything.

**v1 (MVP)**

0. **sharpstack prerequisites** (in sharpstack, in parallel with steps 1–4; see [sharpstack integration](09-sharpstack-integration.md#sharpstack-prerequisites-for-sharpwire-v1)). They must be done before step 6 applies SDN.
1. **Spike.**
   - RouterOS REST on the core config: listening connections with a prefix `remote.address`, `nexthop-choice`, filter-rule ordering by natural key, and the inverse-op rollback script.
   - RouterOS: scheduler behaviour across a reboot, `/export` over REST, and the inverse script after a partial apply.
   - PVE: exactly what SDN apply runs on each node (reload, `interfaces.new` promotion); the SDN lock and rollback endpoints for discarding pending changes; the SDN API round trip (stage, compare pending, apply), regenerating and reloading FRR per node after a `frr.conf.local` change, and the systemd timer plus boot-time restore around `ifreload`.
2. **Intent model and NetBox plugin.** Zones, VNets (with `status`), BGP (including upstream), policy, corosync links, the cluster firewall `enable` and redundancy groups. Also the node bootstrap package (`sharpwire` user, helper, sudoers rule, restore unit).
3. **Read-only drivers.** RouterOS (cores, leafs), Proxmox (SDN, node files, cluster files). Drift detection, with alerts stored in Postgres and shown in the web UI.
4. **Cross-target validation**, run continuously against live state as well as on plans.
5. **Apply path for the cores.** Plans, stale check, minimal web UI approvals, armed rollback, serial rollout.
6. **Apply path for Proxmox.** SDN first, then node files.
7. **Bring up the public zone through sharpwire** in the lab, then in production.

**Later in phase 1**

8. Leaf apply (spike: RouterOS REST on MLAG) and cluster-file apply (spike: node-local corosync restore).
9. Multi-target change sets.
10. Change-class engine, `public-vnet`, plugin allocation, and the sharpstack API.
11. Freezes, Alertmanager (paging) and chat notifications, plus Prometheus metrics.
12. Credential storage in Postgres with envelope encryption.

**Phase 2 (VPC)**

13. VyOS driver, read-only first, then apply (spike: commit-confirm over the HTTP API).
14. Customer model, allocation, `customer-onboard`, and the core NAT-outside config.

## Risks

| Risk | Mitigation |
| --- | --- |
| SDN apply is cluster-wide, so a bad apply hits every node at once and can't be rolled out serially | Validation and the nested lab gate; pending config must equal the plan; controller-driven restore; human approval of every plan in v1; later, `public-vnet` limited to add-only within pre-accepted ranges |
| A second SDN writer: sharpstack or a person in the GUI applies SDN, pushing sharpwire's staged ops or creating drift | The sharpstack prerequisites remove its SDN calls and rights; pending changes block applies; drift alerts |
| Public-zone isolation silently off: cluster firewall disabled or a public NIC without `firewall=1` | Cluster firewall drift detection and the NIC cross-check alert |
| No paging in v1: alerts are seen only in the web UI, and a dead controller raises nothing | Acceptable while every change is human-driven from the UI; Alertmanager is the first later item once sharpwire runs unattended |
| v1 multi-target changes are applied as separate plans, so an operator can apply them in the wrong order | The documented stage order; validation on each plan rejects references to things that don't exist yet; the stale check |
| A host-networking push cuts mgmt and corosync, and HA fencing reboots the node onto the broken config | The armed timer **plus** the boot-time restore; the mgmt-path hard guard; one node at a time with quorum required; IPMI |
| A corosync change splits the cluster, leaving `/etc/pve` read-only | Human only, one link at a time, link1 kept as the second path, a node-local armed restore, and the nested lab |
| Corosync link1 depends on BGP convergence, because it runs over the VTEP loopbacks | link1 is configured at low priority so link0 on mgmt stays primary; link0 is never changed while link1 is down |
| RouterOS has no atomic commit, so a partial apply relies on the rollback script | Rollback armed before the first op; rollback test in CI |
| RouterOS REST shapes and BGP behavior (`nexthop-choice`, multipath) change between 7.x releases | Supported versions pinned per driver; physical lab gate before bumping |
| A leaf plan can cut both the data plane and in-band mgmt | Protected objects, the leaf-pair consistency rules, OOB console |
| A missing VLAN on the peer-link breaks node pairs without BFD noticing | VLAN membership consistency rule, continuously checked against live state |
| The `frr.conf.local` merge silently drops a required line after SDN apply | Post-merge check in health checks and drift |
| **(phase 2)** A VNI or RT mismatch between Proxmox and VyOS blackholes a customer | Both are rendered from one model; the consistency rule runs on live state too |
| **(phase 2)** Overlapping customer ranges collide in conntrack | VyOS version gate on per-VRF conntrack zones |

## Open questions

- [ ] **Draining the active gateway (phase 2).** Should a change to the active gateway shift traffic to the standby first? Draining resets every IPv4 NAT session unless conntrack-sync is on. Proposal: drain only for plans that modify or remove shared BGP or VRF config, never for add-only customer blocks.
- [ ] **DNAT and port forwards (phase 2).** Add them to `customer-onboard`, or keep them human-approved?
- [ ] **VPC model (phase 2).** How sharpstack's VPCs map to customer zones `cNN`: one zone per tenant or per VPC, /48 vs /56, and the 99-customer cap. Settle before phase 2 starts (see [sharpstack integration](09-sharpstack-integration.md#phase-2-vpc-model)).
- [ ] **One operator identity.** Should sharpstack's platform admins also log in with Kanidm, reusing `oidc.py`?
- [ ] **Inter-zone policy (phase 2).** May public and VPC VMs reach each other through the cores at all?
- [ ] **Short CLI alias** (`swire`?).

## Resolved (phase 1)

| Question | Decision |
| --- | --- |
| Upstream BGP | sharpwire manages it fully: ISP sessions, prefix filters and aggregate announcements. The sessions are protected objects |
| Corosync link1 | Added in phase 1, over the VTEP loopbacks at low priority |
| Public VNI range | Tags 200–999, VNets named `vnetNNN` |
| Node access | `sharpwire` user with sudo restricted to the `sharpwire-node` helper |
| Bootstrap | sharpwire ships a versioned bootstrap package; after that, sharpwire pushes helper upgrades itself |
| Rollback timer (T+N) | RouterOS 5 min, Proxmox 10 min; configurable per target type |
| Reconcile interval | 5 minutes |
| Break-glass | Dropped. Freezes are absolute |
