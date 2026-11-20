# Proxmox VE: EVPN/VXLAN with IPv6-only underlay: public and NAT zones

Oct 1, 2026 · @Ole Mathias

## Overview

Each Proxmox node is a VTEP with IPv6-only management and underlay. VMs pick one of two dual-stack EVPN zones by which VNet they attach to:

- **Public zone (`public`).** One shared VRF with public IPv4 and IPv6, and the anycast gateway on every node. Each node hands traffic straight to the CCR2116 cores from the zone's VRF over VLAN 300, using eBGP. There's no VRRP and no middlebox. Customers sharing it are isolated by the Proxmox firewall.
- **Customer NAT zones (`cNN`, one per customer).** Each customer has its own VRF with private IPv4 (ranges may overlap between customers) and a routed IPv6 /56. Traffic exits via the VyOS EVPN gateways, which source-NAT each customer's IPv4 to that customer's single public IPv4 and route IPv6 without NAT.

Neither zone uses Proxmox exit nodes, so neither tenant VRF ever touches the host's default VRF.

Per node:

- **Physical layout.** Each node has a 2×10G LACP bond to two CRS317-1G-16S+ leaf switches running MLAG, which are L2 only. Each CCR2116-12G-4S+ core router is dual-homed to the leaf pair.
- **VLAN 10, mgmt.** Holds the host default route (IPv6 only) and carries GUI, SSH, corosync and apt.
- **VLAN 101 and VLAN 102, underlay.** VLAN 101 is shared by all nodes plus core A, and VLAN 102 by all nodes plus core B, each with a numbered /64. Every node runs eBGP to the core on each VLAN. The VLANs carry VTEP /128 loopbacks only, with ECMP across both.
- **VLAN 300, public handoff.** Shared by all nodes and both cores, and enslaved into `vrf_public` on each node. It's dual-stack.
- **VLAN 310, NAT outside.** Between the VyOS gateways and both cores. Proxmox hosts don't use it.
- **dummy0.** The VTEP and BGP source address (/128).
- **vrf\_public and vrf\_cNN.** The shared public VRF plus one VRF per customer, one L3VNI each.

The cores are the control plane, but not the east-west data plane. When a core re-advertises a node's /128, it keeps that node's VLAN address as the next hop (BGP third-party next hop). Node-to-node VXLAN therefore goes straight across the leafs at L2 line rate. Only traffic to the gateway VTEPs, which is north-south, is routed through a core. Losing a core removes one of the two equivalent shortcuts, not the path.

The guide assumes the latest PVE 9.x (including the 2026 EVPN IPv6-underlay support) and RouterOS 7 on the routers. It covers the Proxmox side only. The router and gateway config is summarised as the expectations Proxmox depends on (Step 6).

## Addressing plan

All host-side addressing is IPv6. IPv4 exists only inside the tenant VRF. The examples use documentation prefixes, so replace them with your own.

| Purpose | Example | Per node (pve1) | Notes |
| --- | --- | --- | --- |
| Mgmt, VLAN 10 | 2001:db8:100::/64 | 2001:db8:100::11/64 | Host default via ::1, MTU 1500 |
| VTEP loopbacks | 2001:db8:ffff::/64 | 2001:db8:ffff::11/128 on dummy0 | Gateway VTEPs ::101 and ::102 |
| Underlay A, VLAN 101 | 2001:db8:101::/64 | 2001:db8:101::11/64 | Core A ::1, MTU 9216 |
| Underlay B, VLAN 102 | 2001:db8:102::/64 | 2001:db8:102::11/64 | Core B ::1, MTU 9216 |
| Public handoff, VLAN 300 | 10.250.0.0/24 + 2001:db8:250::/64 | .11 / ::11 in vrf\_public | Core A .1/::1, core B .2/::2 |
| NAT outside, VLAN 310 | 10.250.1.0/24 + 2001:db8:251::/64 | none | Cores .1/.2, VyOS .101/.102 |
| Node underlay ASN | 4200000000-4294967294 | 4200000011 | Unique per node; applied via `local-as` |
| Public-VRF handoff ASN | 4200000100 | same on all nodes | Identical AS paths make ECMP easy on the cores |
| Core ASNs | 4200000001 / 4200000002 |  | One per core |
| EVPN overlay ASN | 65000 | same on all nodes and gateways | The FRR process ASN; iBGP |
| BGP router-id | 32-bit | 10.255.0.11 | Set explicitly; there's no IPv4 to derive it from |
| Public zone subnets | 198.51.100.0/24 + 2001:db8:70::/48 | anycast gateway on every node | Routed, no NAT; L3VNI 20000 |
| Customer NAT zones | private IPv4 per customer + 2001:db8:60::/48 | anycast gateway on every node | /56 and one NAT IPv4 from 203.0.113.0/24 per customer; L3VNI 100NN (Step 5b) |

Node ASNs must be unique. pve1 learns pve2's loopback with AS path `core, pve2`. With a shared node ASN, pve1 would see its own ASN in the path and drop the route as a loop.

## Step 1: Prerequisites

You need a PVE build that ships EVPN IPv6 underlay support. It was applied upstream in May 2026, so any current 9.x release should have it.

```
pveversion -v | grep -E 'pve-manager|libpve-network|frr'
apt update && apt full-upgrade
```

1. Install and enable FRR on every node: `apt install frr frr-pythontools`. SDN manages `/etc/frr/frr.conf` from now on, so put your own additions in `/etc/frr/frr.conf.local`.
2. Make sure `/etc/network/interfaces` ends with `source /etc/network/interfaces.d/*`. SDN writes its interfaces to `interfaces.d/sdn`.
3. Use ifupdown2 (the PVE default). Test any change with `ifreload -a -n` before applying it.
4. Get out-of-band access (IPMI or console) before you start. You are about to remove the IPv4 mgmt address you are probably connected over.

## Step 2: Host networking

One LACP bond carries everything. Mgmt is VLAN 10 with a static IPv6 address and the only host default route. The two shared underlay VLANs get numbered addresses, and `dummy0` holds the VTEP /128.

```
# /etc/network/interfaces (pve1)
auto lo
iface lo inet loopback

auto eno1
iface eno1 inet manual
    mtu 9216

auto eno2
iface eno2 inet manual
    mtu 9216

# LACP to the CRS317 MLAG pair
auto bond0
iface bond0 inet manual
    bond-slaves eno1 eno2
    bond-mode 802.3ad
    bond-xmit-hash-policy layer3+4
    bond-lacp-rate fast
    bond-miimon 100
    mtu 9216

# VLAN 10: mgmt, host traffic only
auto bond0.10
iface bond0.10 inet6 static
    address 2001:db8:100::11/64
    gateway 2001:db8:100::1
    accept_ra 0
    mtu 1500

# VLAN 101: shared underlay, core A
auto bond0.101
iface bond0.101 inet6 static
    address 2001:db8:101::11/64
    accept_ra 0
    mtu 9216

# VLAN 102: shared underlay, core B
auto bond0.102
iface bond0.102 inet6 static
    address 2001:db8:102::11/64
    accept_ra 0
    mtu 9216

# VTEP / BGP source
auto dummy0
iface dummy0 inet6 static
    link-type dummy
    address 2001:db8:ffff::11/128

source /etc/network/interfaces.d/*
```

pve2 uses `::12` in each prefix, pve3 uses `::13`, and so on. No default route or gateway goes on VLAN 101 or 102.

The layer3+4 hash spreads VXLAN well, because the outer UDP source port carries per-flow entropy. If the leaf pair isn't running MLAG, use `bond-mode active-backup` instead. You keep the same VLAN layout, but all traffic uses one link at a time.

Don't put a `vmbr` on `bond0` for regular VMs. VMs attach only to SDN VNets, so no VM can tag itself into the mgmt or underlay VLANs. The one exception is the restricted bridge for VyOS VMs, if you run VyOS virtualised (Step 6).

VRF routing needs forwarding for both families. IPv6 forwarding is also required for the fabric. Keep RA acceptance off everywhere, since forwarding disables SLAAC behaviour anyway and you use static addressing:

```
# /etc/sysctl.d/90-sdn.conf
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
net.ipv6.conf.default.accept_ra = 0
net.ipv6.conf.all.accept_ra = 0
```

Apply with `sysctl --system && ifreload -a`. Check that the mgmt default survives with `ip -6 route show default`.

## Step 3: Cluster on IPv6

The node name must resolve to its IPv6 mgmt address. pve-cluster and the GUI rely on it.

```
# /etc/hosts (each node lists all nodes)
2001:db8:100::11 pve1.example.net pve1
2001:db8:100::12 pve2.example.net pve2
2001:db8:100::13 pve3.example.net pve3
```

For a new cluster, create it with IPv6 links explicitly:

```
pvecm create mycluster --link0 2001:db8:100::11
pvecm add pve1.example.net --link0 2001:db8:100::12   # on each joining node
```

For an existing cluster, edit `/etc/pve/corosync.conf`: set each `ring0_addr` to the IPv6 mgmt address and bump `config_version`. Change one link at a time, with a second link in place, or you will lose quorum.

Keep link0 on VLAN 10. Because everything shares one bond, corosync competes with VM VXLAN traffic for the same links. Two things limit the risk:

- Mark corosync's VLAN with a high 802.1p priority and honour it on RouterOS: `post-up ip link set bond0.10 type vlan egress-qos-map 0:6` on the mgmt VLAN.
- Add a link1 over the VTEP loopbacks (`2001:db8:ffff::1x`), which are reachable via the underlay, as a low-priority backup. It shares the physical links but takes a different L3 path through both routers. Don't make it primary, or quorum starts depending on BGP convergence.

If the cluster ever shows token timeouts under load, a dedicated physical NIC for corosync is the real fix.

Pin migration traffic to mgmt in `/etc/pve/datacenter.cfg`:

```
migration: secure,network=2001:db8:100::/64
```

## Step 4: Underlay routing

The underlay is handwritten FRR in `/etc/frr/frr.conf.local`, which SDN merges into the generated config on every apply. Don't create an SDN BGP fabric or BGP controller. The fabric is unnumbered, which only works point-to-point, so it can't run on shared VLANs.

FRR allows one BGP instance per VRF, and the EVPN controller already owns `router bgp 65000`. The underlay sessions therefore live in that same process, and each node presents its own ASN to the cores with `local-as … no-prepend replace-as`.

### 4a. FRR underlay config

```
! /etc/frr/frr.conf.local (pve1)
bfd
 profile underlay
  receive-interval 300
  transmit-interval 300
  detect-multiplier 3
 !
 profile overlay
  receive-interval 300
  transmit-interval 300
  detect-multiplier 5
 !
exit
!
ipv6 prefix-list UL-IN seq 10 permit 2001:db8:ffff::/64 ge 128
ipv6 prefix-list UL-OUT seq 10 permit 2001:db8:ffff::11/128
!
route-map UL-IN permit 10
 match ipv6 address prefix-list UL-IN
 set ipv6 next-hop prefer-global
exit
!
route-map UL-OUT permit 10
 match ipv6 address prefix-list UL-OUT
exit
!
router bgp 65000
 bgp router-id 10.255.0.11
 bgp bestpath as-path multipath-relax
 neighbor UL peer-group
 neighbor UL remote-as external
 neighbor UL local-as 4200000011 no-prepend replace-as
 neighbor UL bfd
 neighbor UL bfd profile underlay
 neighbor 2001:db8:101::1 peer-group UL
 neighbor 2001:db8:102::1 peer-group UL
 neighbor VTEP update-source dummy0
 neighbor VTEP bfd profile overlay
 !
 address-family ipv4 unicast
  no neighbor UL activate
 exit-address-family
 !
 address-family ipv6 unicast
  network 2001:db8:ffff::11/128
  neighbor UL activate
  neighbor UL route-map UL-IN in
  neighbor UL route-map UL-OUT out
  maximum-paths 4
 exit-address-family
exit
```

Per node, change the router-id, the `local-as`, the `network` line and the `UL-OUT` prefix:

| Node | router-id | local-as | Loopback |
| --- | --- | --- | --- |
| pve1 | 10.255.0.11 | 4200000011 | 2001:db8:ffff::11/128 |
| pve2 | 10.255.0.12 | 4200000012 | 2001:db8:ffff::12/128 |
| pve3 | 10.255.0.13 | 4200000013 | 2001:db8:ffff::13/128 |

These lines do the following:

- **`UL-IN`** accepts only loopback /128s, so no default route or anything else from the cores can enter the host table.
- **`prefer-global`** makes FRR install the global next hop the core passed on (the other node's VLAN address). Without it, FRR may install the core's link-local next hop instead, and traffic hairpins through the core again.
- **`UL-OUT`** advertises only the node's own loopback, so a node never becomes transit.
- **`multipath-relax`** is needed because the two paths to another node carry different core ASNs. Without it there's no ECMP across VLAN 101 and 102.
- **`update-source dummy0`** sources the EVPN sessions from the loopback, so peers see the address they expect.

After SDN Apply, check the merge with `vtysh -c 'show run'`. Make sure your router-id has replaced the generated one, and that the `VTEP` peer-group name matches.

### 4b. Third-party next hop

The design relies on the cores passing the original node's VLAN address on as the next hop. On RouterOS 7 this is the BGP connection's `nexthop-choice`:

- Start with `default`. This is RFC 4271 behaviour: a next hop on the peer's subnet is kept, anything else becomes the core itself.
- Check on pve1 with `ip -6 route show 2001:db8:ffff::12`. You want two next hops: `via 2001:db8:101::12 dev bond0.101` and `via 2001:db8:102::12 dev bond0.102`.
- If you see `via 2001:db8:101::1` instead, set `nexthop-choice=propagate` on the node-facing connections. Then confirm that the gateway /128s, learned from elsewhere, still arrive with a next hop the node can reach.

The gateway VTEP /128s correctly keep the core as their next hop. North-south traffic is supposed to go through the core.

### 4c. BFD

The `underlay` profile (300 ms × 3, about 0.9 s) covers the case you care about: a core dies while the leafs stay up. That core's sessions drop, its copies of every /128 are withdrawn, and traffic continues on the other VLAN's shortcut. If a node dies, the cores' BFD to that node withdraws its loopback for everyone within the same window.

The `overlay` profile (300 ms × 5) runs multihop BFD on the EVPN sessions. It's deliberately slower than the underlay, so a core failure never tears down EVPN.

Don't go below 300 ms on the underlay. A bond member that stays link-up but stops forwarding takes LACP about 3 s to remove, and tighter timers would drop both VLAN sessions at once. Set RouterOS BFD to 300 ms or lower, because the session negotiates to the slower side.

BFD only watches node ↔ core. A broken L2 path between two nodes, such as VLAN 101 missing on the MLAG peer-link, isn't detected by BGP, because the next hop still looks valid. Correct MLAG VLAN membership is what protects that path (see Step 6).

## Step 5: EVPN controller, zone and VNets

The overlay is iBGP EVPN in ASN 65000 between all node loopbacks and the gateway VTEPs. All zones share the controller. Neither has exit nodes configured: the customer NAT zones exit via VyOS (5b), and the public zone via a handoff built by hand on each node (5c).

### 5a. Controller

Go to SDN → Options → Controllers → Add → EVPN.

```
# /etc/pve/sdn/controllers.cfg
evpn: evpnctl
    asn 65000
    peers 2001:db8:ffff::11,2001:db8:ffff::12,2001:db8:ffff::13,2001:db8:ffff::101,2001:db8:ffff::102
```

- **ASN 65000** is the FRR process ASN, and the per-node underlay ASNs ride on top of it via `local-as` (Step 4).
- **Peers** lists every node loopback plus both gateway VTEPs. All are IPv6, so the VTEPs and EVPN sessions run over IPv6. Each node finds its own VTEP address in this list (the one on `dummy0`) and peers with the rest.
- When you add a node, add its loopback here and add it on the gateways. The underlay needs no change beyond that node's own `frr.conf.local`.

### 5b. Customer NAT zones: one zone per customer

Every customer gets their own EVPN zone, and so their own VRF and L3VNI. There's no route between customers, and private IPv4 ranges may overlap. Each customer gets one public IPv4 for source NAT on VyOS, and a routed IPv6 /56 without NAT.

Numbering scheme (NN = customer, XX = VNet):

| Item | Scheme | Customer 01 |
| --- | --- | --- |
| Zone ID (max 8 characters in PVE) | cNN | c01 |
| VRF on nodes | vrf\_cNN | vrf\_c01 |
| L3VNI | 100NN | 10001 |
| VNet ID | cNNnXX | c01n01 |
| L2 VNI (VNet tag) | 1NNXX | 10101 |
| Private IPv4 | the customer's choice, overlap allowed | 10.0.0.0/24 |
| IPv6 | /56 per customer from 2001:db8:60::/48, /64 per VNet | 2001:db8:60:100::/56, VNet 2001:db8:60:101::/64 |
| Public NAT IPv4 | one /32 per customer from 203.0.113.0/24 | 203.0.113.1 |

The scheme fits 99 customers with 99 VNets each. Widen the VNI ranges before you get close.

Per-customer config, here for c01:

```
# /etc/pve/sdn/zones.cfg
evpn: c01
    controller evpnctl
    vrf-vxlan 10001
    advertise-subnets 1
    mtu 1500
    ipam pve

# /etc/pve/sdn/vnets.cfg
vnet: c01n01
    zone c01
    tag 10101

# /etc/pve/sdn/subnets.cfg
subnet: c01-10.0.0.0-24
    vnet c01n01
    gateway 10.0.0.1

subnet: c01-2001:db8:60:101::-64
    vnet c01n01
    gateway 2001:db8:60:101::1
```

- Leave Exit Nodes and SNAT empty. VyOS owns the exit and the NAT.
- **MTU** is the VM-facing MTU. 1500 is the safe default; over a 9216 underlay you can go up to 9146, since IPv6 VXLAN costs 70 bytes.
- **Advertise subnets** sends each subnet as type-5, so VyOS learns whole prefixes.
- PVE IPAM tracks subnets per zone, so two customers can both have `10.0.0.0/24`. Test once on your build before you promise overlapping ranges.
- Address guests statically or via cloud-init. Don't assume RAs on EVPN VNets.
- Grant each customer access only to their own zone with PVE permissions: `SDN.Use` on `/sdn/zones/cNN`.

Each zone adds a VRF, an L3VNI bridge and one VXLAN interface per VNet on every node. Tens of customers is trivial, but at several hundred, keep an eye on FRR memory and SDN Apply time.

Onboarding a customer is always the same set of steps, so script it against the PVE API (pvesh or Terraform), together with the matching VyOS block from Step 6:

1. Allocate NN, the public IPv4 and the IPv6 /56.
2. Create the zone, VNets and subnets, then apply SDN.
3. Add the VyOS customer block.
4. Run the isolation check from Step 7.

### 5c. Public zone with direct handoff to the cores

The public zone is a second EVPN zone with its own VRF. Every node peers eBGP from that VRF to both cores over VLAN 300: it advertises the public subnets and learns a default for each family. VM traffic leaves the node it runs on, and the VNet's anycast gateway replaces VRRP.

```
# /etc/pve/sdn/zones.cfg
evpn: public
    controller evpnctl
    vrf-vxlan 20000
    advertise-subnets 1
    mtu 1500
    ipam pve

# /etc/pve/sdn/vnets.cfg
vnet: vnet200
    zone public
    tag 200

# /etc/pve/sdn/subnets.cfg
subnet: public-198.51.100.0-26
    vnet vnet200
    gateway 198.51.100.1

subnet: public-2001:db8:70:200::-64
    vnet vnet200
    gateway 2001:db8:70:200::1
```

Leave Exit Nodes and SNAT empty. Apply SDN once so `vrf_public` exists, then add the handoff VLAN to it on every node:

```
# /etc/network/interfaces (pve1)
auto bond0.300
iface bond0.300
    vrf vrf_public
    address 10.250.0.11/24
    address 2001:db8:250::11/64
    accept_ra 0
    mtu 9216
```

Then add the VRF's BGP sessions to `/etc/frr/frr.conf.local`. Only the `bond0.300` addresses differ per node; the BGP config is identical everywhere.

```
ip prefix-list PUB4 seq 10 permit 198.51.100.0/24 le 28
ipv6 prefix-list PUB6 seq 10 permit 2001:db8:70::/48 le 64
ip prefix-list DEF4 seq 10 permit 0.0.0.0/0
ipv6 prefix-list DEF6 seq 10 permit ::/0
!
route-map PUB-OUT permit 10
 match ip address prefix-list PUB4
exit
route-map PUB-OUT permit 20
 match ipv6 address prefix-list PUB6
exit
route-map DEFAULT-ONLY permit 10
 match ip address prefix-list DEF4
exit
route-map DEFAULT-ONLY permit 20
 match ipv6 address prefix-list DEF6
 set ipv6 next-hop prefer-global
exit
route-map NO-DEFAULT deny 10
 match ip address prefix-list DEF4
exit
route-map NO-DEFAULT deny 20
 match ipv6 address prefix-list DEF6
exit
route-map NO-DEFAULT permit 100
exit
!
router bgp 65000 vrf vrf_public
 neighbor CORE peer-group
 neighbor CORE remote-as external
 neighbor CORE local-as 4200000100 no-prepend replace-as
 neighbor CORE bfd
 neighbor CORE bfd profile underlay
 neighbor CORE6 peer-group
 neighbor CORE6 remote-as external
 neighbor CORE6 local-as 4200000100 no-prepend replace-as
 neighbor CORE6 bfd
 neighbor CORE6 bfd profile underlay
 neighbor 10.250.0.1 peer-group CORE
 neighbor 10.250.0.2 peer-group CORE
 neighbor 2001:db8:250::1 peer-group CORE6
 neighbor 2001:db8:250::2 peer-group CORE6
 !
 address-family ipv4 unicast
  redistribute connected
  neighbor CORE activate
  neighbor CORE6 deactivate
  neighbor CORE route-map DEFAULT-ONLY in
  neighbor CORE route-map PUB-OUT out
  maximum-paths 2
 exit-address-family
 !
 address-family ipv6 unicast
  redistribute connected
  neighbor CORE6 activate
  neighbor CORE6 route-map DEFAULT-ONLY in
  neighbor CORE6 route-map PUB-OUT out
  maximum-paths 2
 exit-address-family
 !
 address-family l2vpn evpn
  advertise ipv4 unicast route-map NO-DEFAULT
  advertise ipv6 unicast route-map NO-DEFAULT
 exit-address-family
exit
```

These lines do the following:

- **`PUB-OUT` on the way out.** The VRF also holds /32 and /128 host routes for VMs on other nodes, imported from EVPN. Without this filter, a node would advertise those to the cores and attract traffic for VMs it doesn't host. The `le` lengths cover the VNet subnets only; tighten them to your real subnet sizes.
- **`DEFAULT-ONLY` on the way in.** Each node accepts only the two defaults from the cores, and nothing else enters the public VRF.
- **`NO-DEFAULT` on the EVPN export.** Every node already has its own default from the cores, so they must not send it to each other as type-5. Merge this with whatever `advertise` lines SDN generates for the VRF, and check `vtysh -c 'show run'` after Apply.
- **Shared handoff ASN 4200000100.** The cores see an identical AS path from every node, so they can ECMP the public subnets across all nodes. Nodes never need each other's routes from the cores, so the shared ASN causes no loop drops.

Return traffic can arrive at any node advertising the subnet. If the VM lives elsewhere, the node forwards the packet over VXLAN using the EVPN host route. That costs one extra hop at leaf line rate and needs no config.

## Step 6: What Proxmox expects from the routers and gateway

Proxmox works only if the far side provides the following. Use this as the acceptance checklist for the router and gateway config.

**CRS317-1G-16S+ leaf pair (L2 only)**

- MLAG between the two leafs over a peer-link of at least 2×10G. Each Proxmox node and each core has one MLAG LACP bond (fast rate), with the same `mlag-id` on both leafs and `vlan-filtering=yes` on the bridge.
- VLAN membership:

  | VLAN | Node bonds | Core A | Core B | VyOS |
  | --- | --- | --- | --- | --- |
  | 10 mgmt | all | yes | yes | (mgmt) |
  | 101 underlay A | all | yes | no | yes |
  | 102 underlay B | all | no | yes | yes |
  | 300 public handoff | all | yes | yes | no |
  | 310 NAT outside | only VyOS hosts | yes | yes | yes |
- The peer-port tagged in every VLAN. Node-to-node shortcuts and VLAN 300 return traffic cross the peer-link whenever LACP hashes land on different leafs, and BFD can't see a missing VLAN there.
- Size the peer-link for east-west load, since it carries node-to-node traffic as well as core traffic.
- L2MTU of at least 9220 on every port (9216 plus the 4-byte VLAN tag); Appendix A uses 9500. The leafs' own MTU only affects their mgmt address.
- No IP addresses or routing on the leafs, apart from their own mgmt.

**CCR2116-12G-4S+ core routers**

- **Underlay.**
  - Core A has `vlan101` with `2001:db8:101::1/64`, and core B has `vlan102` with `2001:db8:102::1/64`, both MTU 9216.
  - One eBGP connection per node to its global VLAN address, IPv6 unicast, BFD at 300 ms × 3, with a distinct remote ASN per node.
  - `nexthop-choice` kept so node /128s go out with the originating node's VLAN address as next hop (Step 4b). Verify this from a node.
  - Advertise only node and gateway VTEP /128s inside `2001:db8:ffff::/64`, with no default. Accept from each node only its own /128.
- **Public handoff (VLAN 300).**
  - Both cores on `vlan300` (core A `.1`/`::1`, core B `.2`/`::2`).
  - eBGP to every node's handoff address, IPv4 and IPv6 sessions, remote ASN 4200000100, BFD at 300 ms × 3.
  - Advertise `0.0.0.0/0` and `::/0` only.
  - Accept only the public prefixes (`198.51.100.0/24` and longer, `2001:db8:70::/48` and longer), and announce the aggregates upstream.
  - Enable BGP ECMP across the nodes, and confirm your RouterOS version actually installs multiple BGP paths. Without ECMP, all return traffic for a subnet enters through one node and fans out over VXLAN. That still works, but that node carries it all.
- **NAT outside (VLAN 310).**
  - Both cores on `vlan310`, with eBGP or static routing to the VyOS pair.
  - Learn from VyOS the NAT pool `203.0.113.0/24` (one /32 per customer, aggregated) and the customer IPv6 block `2001:db8:60::/48`.
- **Policy between zones.** Public and NAT VMs reach each other only through the cores, so decide here whether they may. Stateful rules must allow for asymmetric paths across the two cores; stateless ACLs avoid the issue.
- **No EVPN or tenant VRFs from Proxmox on the cores.** The public zone arrives as plain routes on VLAN 300, and the NAT zone as plain routes from VyOS.

**EVPN gateway (VyOS)**

Shared, configured once:

- A VTEP source of `2001:db8:ffff::101`/`::102`. VyOS joins underlay VLANs 101 and 102 like a node, peering with the cores, which then carry its /128.
- An iBGP L2VPN EVPN session in ASN 65000 to every node loopback, with BFD. VyOS must never import the public zone's L3VNI `20000`.
- An `outside` VRF holding VLAN 310 towards both cores, with eBGP there. It advertises the aggregates `203.0.113.0/24` and `2001:db8:60::/48`, and learns `0.0.0.0/0` and `::/0`. Private customer ranges never enter `outside`.
- Separate connection tracking per customer VRF (conntrack zones). Two customers can send identical private flows, for example both from `10.0.0.5:40000` to the same DNS server. Without per-VRF zones those collide in one conntrack table. Confirm your VyOS version separates them before onboarding overlapping customers.
- If VyOS runs as VMs on these Proxmox nodes, give them NICs on VLANs 101, 102 and 310 through a dedicated VLAN-aware bridge with `bridge-vids 101 102 310`. Create that bridge on the hosting nodes only, and keep the two VyOS VMs on different nodes with HA rules.

Per customer (template, here c01 with public IPv4 `203.0.113.1`):

1. A VRF `c01` with L3VNI `10001`, route targets matching Proxmox (auto `65000:10001`), and type-5 originate of `0.0.0.0/0` and `::/0`.
2. A veth pair between `c01` and `outside`, with a /31 from `100.64.0.0/22` and a /127 (or link-locals).
3. In `c01`, defaults for both families via the veth towards `outside`.
4. In `outside`, `203.0.113.1/32` and `2001:db8:60:100::/56` routed via the veth towards `c01`.
5. Source NAT on the customer-side veth (outbound interface), from any source to `203.0.113.1`. Optional DNAT or port forwards for inbound go on the same address.
6. No other routes. Customers can reach each other only via their public addresses, through `outside`, exactly as they would from the internet.

The veth design keeps NAT per customer and decides which customer a returning packet belongs to by its public /32. Overlapping private ranges never meet in a routing table.

Keep RouterOS out of the EVPN gateway role for now. As of the 7.24 betas, users report that type-5 routes from Proxmox reach the RouterOS EVPN table but are not installed into the VRF/FIB ([MikroTik forum](https://forum.mikrotik.com/t/routeros-7-24beta2-evpn-type-5-prefix-route-is-visible-in-evpn-rib-but-not-imported-into-vrf-fib/271144)).

## Step 7: Verification

Work bottom-up. Each check assumes the one before it passed.

| # | Check | Command | Expected |
| --- | --- | --- | --- |
| 1 | Host table is clean | `ip -6 route; ip -4 route` | IPv6 default via mgmt only; other nodes' /128s via their own VLAN 101/102 addresses; gateway /128s via the cores or VyOS's VLAN address; IPv4 table empty apart from local |
| 2 | Underlay sessions | `vtysh -c 'show bgp ipv6 unicast summary'` | 2 sessions (core A on VLAN 101, core B on VLAN 102) Established |
| 3 | Third-party next hop | `ip -6 route show 2001:db8:ffff::12` | Two next hops, `2001:db8:101::12` and `2001:db8:102::12`, never `::1` |
| 4 | BFD | `vtysh -c 'show bfd peers brief'` | Core underlay sessions, VLAN 300 sessions and all overlay peers up |
| 5 | Underlay MTU | `ping -6 -M do -s 9100 -I 2001:db8:ffff::11 2001:db8:ffff::12` | Replies, no fragmentation errors; repeat towards a gateway VTEP |
| 6 | Overlay sessions | `vtysh -c 'show bgp l2vpn evpn summary'` | Every node and gateway loopback Established, over IPv6 |
| 7 | VNI state | `vtysh -c 'show evpn vni'` | Public VNIs 200 and 20000, plus each customer's 1NNXX and 100NN, up; remote VTEPs as IPv6 |
| 8 | Customer zone default | `ip route show vrf vrf_c01; ip -6 route show vrf vrf_c01` | v4 and v6 default via the VyOS VTEPs |
| 9 | Public handoff | `vtysh -c 'show bgp vrf vrf_public summary'` | 4 sessions (2 cores × v4/v6) Established; each receives 1 prefix, the default |
| 10 | Public zone default | `ip route show vrf vrf_public; ip -6 route show vrf vrf_public` | Defaults via `10.250.0.1`/`.2` and `2001:db8:250::1`/`::2` on `bond0.300`, never via a VTEP |
| 11 | Public advertisement | `vtysh -c 'show bgp vrf vrf_public ipv4 neighbors 10.250.0.1 advertised-routes'` | Only the public subnets, no /32s; same for IPv6 |
| 12 | No leaking | `ip -6 route show default; ip route show default` | Only the mgmt v6 default; no v4 default in the main table |
| 13 | Data plane | ping from VMs in each zone to a VM on another node and to the internet, on v4 and v6 | All succeed. Public VMs show their own address externally; customer VMs show their customer's single public IPv4 |
| 14 | Customer isolation | from a c01 VM, ping a c02 VM's private address, including one in an overlapping range | No reply. c02 is reachable only via its public IPv4 and DNAT, if configured |

Bond check: `cat /proc/net/bonding/bond0` should show both members up with the same aggregator ID, which confirms MLAG formed one LAG.

Failover tests:

1. Pull one bond member, or reboot one leaf. LACP and MLAG should carry on via the other leaf. All BGP sessions stay up and VM traffic continues.
2. Power off core A with both leafs up. The VLAN 101 and VLAN 300 sessions to core A should drop within about 0.9 s. Node-to-node traffic continues via the VLAN 102 shortcuts, public traffic via core B on VLAN 300, NAT traffic via core B, and EVPN sessions stay up.
3. Power off a node. Within about 0.9 s, the cores should drop its /128 and its public-subnet advertisements, and return traffic should shift to the remaining nodes.
4. Stop one VyOS gateway. NAT-zone traffic should move to the other one. Existing IPv4 NAT sessions break unless the gateways sync connection state.
5. Watch corosync during all tests with `corosync-cfgtool -s` and `journalctl -u corosync -f`. No token loss is expected.

## Troubleshooting and gotchas

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Node /128s show `via 2001:db8:101::1` (via the core) | The core rewrites the next hop, or FRR picked the core's link-local | Check `nexthop-choice` on the core (Step 4b), and that `set ipv6 next-hop prefer-global` is in `UL-IN` |
| Only one next hop per remote node, no ECMP | Different core ASNs in the two paths | `bgp bestpath as-path multipath-relax` and `maximum-paths` under ipv6 unicast |
| A remote node's /128 is missing entirely | Shared node ASN, so the route is dropped as an AS-path loop | Use a unique `local-as` per node |
| Underlay sessions up but no routes accepted | eBGP policy missing or the prefix-list doesn't match | Check `show bgp ipv6 unicast neighbors <core> received-routes` against `UL-IN` |
| EVPN sessions won't establish | Missing `update-source dummy0`, so sessions leave from the VLAN address | Check `show run` after SDN Apply; the merge must contain the line |
| EVPN up, but no type-5 default in `vrf_cNN` | Route-target mismatch with the gateway | Compare `show bgp l2vpn evpn vni` on both ends; align RTs or set them explicitly |
| Some node pairs work, others don't | VLAN 101/102 missing on the MLAG peer-link or one node's bond | Fix VLAN membership on the leafs; BFD won't catch this |
| Small packets work, large ones hang | Underlay MTU below VM MTU + 70 somewhere on the path | Ping with DF from loopback to loopback (Step 7, check 5) |
| Custom FRR lines vanish after SDN Apply | They were edited into `frr.conf` | Put them only in `/etc/frr/frr.conf.local` |
| BGP/BFD/VXLAN blocked with the PVE firewall on | Host rules drop underlay traffic | Allow TCP 179, UDP 3784-3785, UDP 4784 (multihop BFD) and UDP 4789 on VLANs 101/102 |
| apt, ACME or other updates fail | The endpoint is IPv4-only | NAT64/DNS64 or an HTTP proxy on the mgmt network |
| Ceph won't start on IPv6 | It binds both families by default | `ms_bind_ipv6 = true`, `ms_bind_ipv4 = false` |

Public and customer zone specifics:

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `bond0.300` comes up in the default VRF, or not at all | `vrf_public` didn't exist yet when ifupdown ran | Apply SDN first, then `ifreload -a`; test a full reboot |
| Public VMs reach the internet, but inbound traffic sometimes lands on the wrong node | Expected: the cores ECMP to any node advertising the subnet | Nothing to fix. The node forwards over VXLAN; check `show bgp vrf vrf_public` holds the EVPN host route |
| Cores learn /32 or /128 routes from nodes | `PUB-OUT` missing or too loose | Apply it as the outbound route-map on `CORE`/`CORE6`, tightened to the real subnet sizes |
| Public VM traffic goes through another node's VLAN 300 | Nodes exchanged defaults as type-5 | Check that `NO-DEFAULT` is attached to the VRF's `advertise` lines after the SDN merge |
| All public return traffic enters through one node | The cores aren't doing BGP ECMP | Enable multipath on the cores and verify that multiple next hops are installed |
| A new customer zone has no default | VyOS customer block missing, or an RT mismatch | Check that VyOS has VRF `cNN` with L3VNI 100NN and auto RT `65000:100NN` |
| Two customers with the same private range break each other's sessions | Shared conntrack on VyOS | Enable per-VRF conntrack zones on VyOS |
| A customer reaches another customer's private addresses | Routes leaked between customer VRFs, or the zone was mistakenly put in another customer's VRF | Customer VRFs may only have their veth default; check RT imports on VyOS and the zone each VNet belongs to |
| Customer VMs reach public-zone VMs one way only | Stateful filtering on one core sees half the flow | Use stateless ACLs between zones, or pin inter-zone traffic to one core |

Other things to watch:

- Keep the host firewall rules for `vnet` interfaces separate from VLANs 101/102. VM traffic never appears there unencapsulated.
- Stage changes in `/etc/pve/sdn/` and apply them cluster-wide in one go. Half-applied SDN config across nodes is the most common cause of asymmetric paths.
- Keep `frr.conf.local` in config management (Ansible or similar). It's the one per-node piece SDN doesn't generate for you.
- Set the bond MTU to the largest of its VLANs. A VLAN can't exceed its parent bond.

## Appendix A: RouterOS base config

These are starting configs for RouterOS 7, matching the addressing plan. Syntax shifts between 7.x releases, so paste section by section and check each with `print` before moving on. Port names are examples: leaf ports 1–3 go to pve1–3, 13–14 to the cores, and 15–16 form the peer-link. Each core uses ports 1–2 towards the leafs.

### A.1 Leaf A (CRS317-1G-16S+)

On RouterOS, `l2mtu` is the largest frame the port hardware carries, counted without the 14-byte Ethernet header but including VLAN tags. `mtu` is the IP packet size for traffic the box routes or terminates. A tagged 9216-byte packet needs an `l2mtu` of at least 9220. Check each port's `max-l2mtu` with `/interface ethernet print detail` and stay at or below it.

```
/interface ethernet
set [find where name~"sfp-sfpplus"] l2mtu=9500 mtu=9216

/interface bonding
add name=peer-link slaves=sfp-sfpplus15,sfp-sfpplus16 mode=802.3ad lacp-rate=1sec mtu=9216
add name=bond-pve1 slaves=sfp-sfpplus1 mode=802.3ad lacp-rate=1sec mlag-id=11 mtu=9216
add name=bond-pve2 slaves=sfp-sfpplus2 mode=802.3ad lacp-rate=1sec mlag-id=12 mtu=9216
add name=bond-pve3 slaves=sfp-sfpplus3 mode=802.3ad lacp-rate=1sec mlag-id=13 mtu=9216
add name=bond-coreA slaves=sfp-sfpplus13 mode=802.3ad lacp-rate=1sec mlag-id=21 mtu=9216
add name=bond-coreB slaves=sfp-sfpplus14 mode=802.3ad lacp-rate=1sec mlag-id=22 mtu=9216

/interface bridge
add name=bridge vlan-filtering=yes

/interface bridge mlag
set bridge=bridge peer-port=peer-link

/interface bridge port
add bridge=bridge interface=peer-link
add bridge=bridge interface=bond-pve1 frame-types=admit-only-vlan-tagged
add bridge=bridge interface=bond-pve2 frame-types=admit-only-vlan-tagged
add bridge=bridge interface=bond-pve3 frame-types=admit-only-vlan-tagged
add bridge=bridge interface=bond-coreA frame-types=admit-only-vlan-tagged
add bridge=bridge interface=bond-coreB frame-types=admit-only-vlan-tagged

/interface bridge vlan
add bridge=bridge vlan-ids=10  tagged=bridge,peer-link,bond-pve1,bond-pve2,bond-pve3,bond-coreA,bond-coreB
add bridge=bridge vlan-ids=101 tagged=peer-link,bond-pve1,bond-pve2,bond-pve3,bond-coreA
add bridge=bridge vlan-ids=102 tagged=peer-link,bond-pve1,bond-pve2,bond-pve3,bond-coreB
add bridge=bridge vlan-ids=300 tagged=peer-link,bond-pve1,bond-pve2,bond-pve3,bond-coreA,bond-coreB
# 310 only on bonds of nodes that host VyOS
add bridge=bridge vlan-ids=310 tagged=peer-link,bond-pve1,bond-pve2,bond-coreA,bond-coreB

# Leaf management on VLAN 10
/interface vlan
add name=mgmt interface=bridge vlan-id=10
/ipv6 address
add address=2001:db8:100::f1/64 interface=mgmt advertise=no
```

Leaf B is identical apart from its mgmt address (`::f2`). The `mlag-id` of each bond must be the same on both leafs. Set the bridge STP priority and ICCP options as MikroTik's MLAG documentation describes for your version. The peer-link must not be a member of any MLAG bond.

### A.2 Core A (CCR2116-12G-4S+)

Interfaces and addressing:

```
/interface ethernet
set sfp-sfpplus1,sfp-sfpplus2 l2mtu=9500 mtu=9216

/interface bonding
add name=bond-leafs slaves=sfp-sfpplus1,sfp-sfpplus2 mode=802.3ad lacp-rate=1sec transmit-hash-policy=layer-3-and-4 mtu=9216

/interface vlan
add name=vlan10  interface=bond-leafs vlan-id=10  mtu=1500
add name=vlan101 interface=bond-leafs vlan-id=101 mtu=9216
add name=vlan300 interface=bond-leafs vlan-id=300 mtu=9216
add name=vlan310 interface=bond-leafs vlan-id=310 mtu=1500

/ipv6 address
add address=2001:db8:101::1/64 interface=vlan101 advertise=no
add address=2001:db8:250::1/64 interface=vlan300 advertise=no
add address=2001:db8:251::1/64 interface=vlan310 advertise=no
/ip address
add address=10.250.0.1/24 interface=vlan300
add address=10.250.1.1/24 interface=vlan310
```

BGP instance, BFD and filters:

```
/routing bgp instance
add name=core as=4200000001 router-id=10.255.255.1

/routing bfd configuration
add interfaces=vlan101,vlan300,vlan310 min-rx=300ms min-tx=300ms multiplier=3

/routing filter rule
# underlay: accept each node's own /128 only (one chain per peer)
add chain=ul-in-pve1  rule="if (dst == 2001:db8:ffff::11/128) { accept }"
add chain=ul-in-pve1  rule="reject"
add chain=ul-in-vyos1 rule="if (dst == 2001:db8:ffff::101/128) { accept }"
add chain=ul-in-vyos1 rule="reject"
# underlay: send only VTEP /128s
add chain=ul-out rule="if (dst in 2001:db8:ffff::/64 && dst-len == 128) { accept }"
add chain=ul-out rule="reject"
# public handoff: accept only public subnets from nodes
add chain=pub-in rule="if (dst in 198.51.100.0/24 && dst-len in 24-28) { accept }"
add chain=pub-in rule="if (dst in 2001:db8:70::/48 && dst-len in 48-64) { accept }"
add chain=pub-in rule="reject"
# NAT outside: accept customer NAT /32s and IPv6 blocks from VyOS
add chain=nat-in rule="if (dst in 203.0.113.0/24 && dst-len in 24-32) { accept }"
add chain=nat-in rule="if (dst in 2001:db8:60::/48 && dst-len in 48-56) { accept }"
add chain=nat-in rule="reject"
# towards public handoff and VyOS: defaults only
add chain=default-out rule="if (dst == 0.0.0.0/0 || dst == ::/0) { accept }"
add chain=default-out rule="reject"
```

BGP connections:

```
/routing bgp connection
# underlay, one per node and VyOS (template shown for pve1 and vyos1)
add name=ul-pve1 instance=core afi=ipv6 local.role=ebgp \
    local.address=2001:db8:101::1 remote.address=2001:db8:101::11 remote.as=4200000011 \
    use-bfd=yes nexthop-choice=default input.filter=ul-in-pve1 output.filter-chain=ul-out
add name=ul-vyos1 instance=core afi=ipv6 local.role=ebgp \
    local.address=2001:db8:101::1 remote.address=2001:db8:101::101 remote.as=4200000101 \
    use-bfd=yes nexthop-choice=default input.filter=ul-in-vyos1 output.filter-chain=ul-out

# public handoff: one listening connection per family covers all nodes (shared ASN)
add name=pub4 instance=core afi=ip local.role=ebgp \
    local.address=10.250.0.1 remote.address=10.250.0.0/24 remote.as=4200000100 \
    use-bfd=yes input.filter=pub-in output.filter-chain=default-out output.default-originate=always
add name=pub6 instance=core afi=ipv6 local.role=ebgp \
    local.address=2001:db8:250::1 remote.address=2001:db8:250::/64 remote.as=4200000100 \
    use-bfd=yes input.filter=pub-in output.filter-chain=default-out output.default-originate=always

# NAT outside towards both VyOS (template shown for vyos1)
add name=nat4-vyos1 instance=core afi=ip local.role=ebgp \
    local.address=10.250.1.1 remote.address=10.250.1.101 remote.as=4200000201 \
    use-bfd=yes input.filter=nat-in output.filter-chain=default-out output.default-originate=always
add name=nat6-vyos1 instance=core afi=ipv6 local.role=ebgp \
    local.address=2001:db8:251::1 remote.address=2001:db8:251::101 remote.as=4200000201 \
    use-bfd=yes input.filter=nat-in output.filter-chain=default-out output.default-originate=always
```

Core B mirrors this with VLAN 102, `.2`/`::2` addresses, ASN 4200000002 and router-id `10.255.255.2`. Notes on this config:

- **Underlay chains.** Repeat the `ul-in-*` chain and connection for every node and VyOS. They're the only per-node lines on the cores.
- **Public handoff.** A connection with a prefix as `remote.address` listens for any node in that range, so adding a node needs no core change.
- **Verify on the cores:**
  - `/routing route print where bgp` shows node /128s with the node's VLAN address as gateway.
  - The public subnets are installed with multiple gateways (ECMP).
  - Neither the private customer ranges nor the handoff /24 is announced upstream.
- **Mgmt gateway.** `2001:db8:100::1` is VRRPv3 on `vlan10` across both cores, or a firewall. Keep it outside the BGP config above.
- **Upstream.** Announcing `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8:70::/48` and `2001:db8:60::/48` to your transit is site-specific and not covered here.

## Appendix B: VyOS base config

This is a starting config for current VyOS (1.5 or rolling), shown for `vyos1`. `vyos2` is identical apart from its addresses (`::102`, `.102`), its underlay ASN (4200000102) and the backup policy in B.4. `eth1` is a trunk carrying VLANs 101, 102 and 310. When VyOS runs as a VM, that's the NIC on the restricted `bridge-vids 101 102 310` bridge.

### B.1 Interfaces and BFD

```
set interfaces ethernet eth1 mtu 9216
set interfaces ethernet eth1 vif 101 address 2001:db8:101::101/64
set interfaces ethernet eth1 vif 101 mtu 9216
set interfaces ethernet eth1 vif 102 address 2001:db8:102::101/64
set interfaces ethernet eth1 vif 102 mtu 9216
set interfaces ethernet eth1 vif 310 vrf outside
set interfaces ethernet eth1 vif 310 address 10.250.1.101/24
set interfaces ethernet eth1 vif 310 address 2001:db8:251::101/64
set interfaces dummy dum0 address 2001:db8:ffff::101/128

set protocols bfd profile underlay interval receive 300
set protocols bfd profile underlay interval transmit 300
set protocols bfd profile underlay interval multiplier 3
set protocols bfd profile overlay interval receive 300
set protocols bfd profile overlay interval transmit 300
set protocols bfd profile overlay interval multiplier 5
```

### B.2 Underlay and EVPN (default VRF)

This mirrors the Proxmox `frr.conf.local`: process ASN 65000, `local-as` towards the cores, and iBGP EVPN to the node loopbacks.

```
set policy prefix-list6 VTEPS rule 10 action permit
set policy prefix-list6 VTEPS rule 10 prefix 2001:db8:ffff::/64
set policy prefix-list6 VTEPS rule 10 ge 128
set policy prefix-list6 SELF rule 10 action permit
set policy prefix-list6 SELF rule 10 prefix 2001:db8:ffff::101/128
set policy route-map UL-IN rule 10 action permit
set policy route-map UL-IN rule 10 match ipv6 address prefix-list VTEPS
set policy route-map UL-IN rule 10 set ipv6-next-hop prefer-global
set policy route-map UL-OUT rule 10 action permit
set policy route-map UL-OUT rule 10 match ipv6 address prefix-list SELF

set protocols bgp system-as 65000
set protocols bgp parameters router-id 10.255.0.101
set protocols bgp parameters bestpath as-path multipath-relax
set protocols bgp address-family ipv6-unicast network 2001:db8:ffff::101/128
set protocols bgp address-family ipv6-unicast maximum-paths ebgp 4

set protocols bgp neighbor 2001:db8:101::1 remote-as 4200000001
set protocols bgp neighbor 2001:db8:102::1 remote-as 4200000002
set protocols bgp neighbor 2001:db8:101::1 local-as 4200000101 no-prepend replace-as
set protocols bgp neighbor 2001:db8:102::1 local-as 4200000101 no-prepend replace-as
set protocols bgp neighbor 2001:db8:101::1 bfd profile underlay
set protocols bgp neighbor 2001:db8:102::1 bfd profile underlay
set protocols bgp neighbor 2001:db8:101::1 address-family ipv6-unicast route-map import UL-IN
set protocols bgp neighbor 2001:db8:101::1 address-family ipv6-unicast route-map export UL-OUT
set protocols bgp neighbor 2001:db8:102::1 address-family ipv6-unicast route-map import UL-IN
set protocols bgp neighbor 2001:db8:102::1 address-family ipv6-unicast route-map export UL-OUT

set protocols bgp peer-group VTEP remote-as internal
set protocols bgp peer-group VTEP update-source dum0
set protocols bgp peer-group VTEP address-family l2vpn-evpn
set protocols bgp address-family l2vpn-evpn advertise-all-vni
# one per node loopback and the other gateway
set protocols bgp neighbor 2001:db8:ffff::11 peer-group VTEP
set protocols bgp neighbor 2001:db8:ffff::11 bfd profile overlay
set protocols bgp neighbor 2001:db8:ffff::12 peer-group VTEP
set protocols bgp neighbor 2001:db8:ffff::12 bfd profile overlay
set protocols bgp neighbor 2001:db8:ffff::13 peer-group VTEP
set protocols bgp neighbor 2001:db8:ffff::13 bfd profile overlay
set protocols bgp neighbor 2001:db8:ffff::102 peer-group VTEP
set protocols bgp neighbor 2001:db8:ffff::102 bfd profile overlay
```

### B.3 Outside VRF (shared)

The outside VRF peers with both cores on VLAN 310. It announces only the aggregates, which are held up by blackhole routes, and accepts only the defaults.

```
set vrf name outside table 100
set vrf name outside protocols static route 203.0.113.0/24 blackhole
set vrf name outside protocols static route6 2001:db8:60::/48 blackhole

set policy prefix-list AGG4 rule 10 action permit
set policy prefix-list AGG4 rule 10 prefix 203.0.113.0/24
set policy prefix-list6 AGG6 rule 10 action permit
set policy prefix-list6 AGG6 rule 10 prefix 2001:db8:60::/48
set policy prefix-list DEF4 rule 10 action permit
set policy prefix-list DEF4 rule 10 prefix 0.0.0.0/0
set policy prefix-list6 DEF6 rule 10 action permit
set policy prefix-list6 DEF6 rule 10 prefix ::/0
set policy route-map OUT-AGG rule 10 action permit
set policy route-map OUT-AGG rule 10 match ip address prefix-list AGG4
set policy route-map OUT-AGG rule 20 action permit
set policy route-map OUT-AGG rule 20 match ipv6 address prefix-list AGG6
set policy route-map IN-DEF rule 10 action permit
set policy route-map IN-DEF rule 10 match ip address prefix-list DEF4
set policy route-map IN-DEF rule 20 action permit
set policy route-map IN-DEF rule 20 match ipv6 address prefix-list DEF6

set vrf name outside protocols bgp system-as 4200000201
set vrf name outside protocols bgp address-family ipv4-unicast network 203.0.113.0/24
set vrf name outside protocols bgp address-family ipv6-unicast network 2001:db8:60::/48
# core A shown; repeat for core B (10.250.1.2 / 2001:db8:251::2, AS 4200000002)
set vrf name outside protocols bgp neighbor 10.250.1.1 remote-as 4200000001
set vrf name outside protocols bgp neighbor 10.250.1.1 bfd profile underlay
set vrf name outside protocols bgp neighbor 10.250.1.1 address-family ipv4-unicast route-map import IN-DEF
set vrf name outside protocols bgp neighbor 10.250.1.1 address-family ipv4-unicast route-map export OUT-AGG
set vrf name outside protocols bgp neighbor 2001:db8:251::1 remote-as 4200000001
set vrf name outside protocols bgp neighbor 2001:db8:251::1 bfd profile underlay
set vrf name outside protocols bgp neighbor 2001:db8:251::1 address-family ipv6-unicast route-map import IN-DEF
set vrf name outside protocols bgp neighbor 2001:db8:251::1 address-family ipv6-unicast route-map export OUT-AGG
```

### B.4 Active/standby between vyos1 and vyos2

Both gateways NAT each customer to the same public /32. If Proxmox nodes or the cores split flows across both, returning packets reach the gateway that has no conntrack entry, and the flow dies. Make vyos1 primary in both directions, and on `vyos2` only:

```
# Proxmox prefers vyos1's type-5 defaults
set policy route-map EVPN-BACKUP rule 10 action permit
set policy route-map EVPN-BACKUP rule 10 set local-preference 50
set protocols bgp peer-group VTEP address-family l2vpn-evpn route-map export EVPN-BACKUP

# The cores prefer vyos1 for the NAT pool and IPv6 block
set policy route-map OUT-AGG rule 10 set as-path prepend '4200000201 4200000201'
set policy route-map OUT-AGG rule 20 set as-path prepend '4200000201 4200000201'
```

Failover then takes about BFD time, but existing IPv4 NAT sessions reset. For stateful failover, add `service conntrack-sync` between the two gateways.

### B.5 Per customer (template, c01)

```
# VRF and L3VNI
set vrf name c01 table 1001
set vrf name c01 vni 10001
set interfaces vxlan vxlan10001 vni 10001
set interfaces vxlan vxlan10001 source-address 2001:db8:ffff::101
set interfaces vxlan vxlan10001 parameters nolearning
set interfaces vxlan vxlan10001 mtu 1500
set interfaces bridge br10001 member interface vxlan10001
set interfaces bridge br10001 vrf c01

set vrf name c01 protocols bgp system-as 65000
set vrf name c01 protocols bgp address-family l2vpn-evpn advertise ipv4 unicast
set vrf name c01 protocols bgp address-family l2vpn-evpn advertise ipv6 unicast
set vrf name c01 protocols bgp address-family l2vpn-evpn default-originate ipv4
set vrf name c01 protocols bgp address-family l2vpn-evpn default-originate ipv6

# veth between c01 and outside
set interfaces virtual-ethernet veth1001 peer-name veth1001o
set interfaces virtual-ethernet veth1001 vrf c01
set interfaces virtual-ethernet veth1001 address 100.64.0.0/31
set interfaces virtual-ethernet veth1001 address fd00:64:1::/127
set interfaces virtual-ethernet veth1001o peer-name veth1001
set interfaces virtual-ethernet veth1001o vrf outside
set interfaces virtual-ethernet veth1001o address 100.64.0.1/31
set interfaces virtual-ethernet veth1001o address fd00:64:1::1/127

# routing: customer default to outside, public /32 and /56 back
set vrf name c01 protocols static route 0.0.0.0/0 next-hop 100.64.0.1
set vrf name c01 protocols static route6 ::/0 next-hop fd00:64:1::1
set vrf name outside protocols static route 203.0.113.1/32 next-hop 100.64.0.0
set vrf name outside protocols static route6 2001:db8:60:100::/56 next-hop fd00:64:1::

# IPv4 source NAT to the customer's address (IPv6 is not translated)
set nat source rule 1001 outbound-interface name veth1001
set nat source rule 1001 translation address 203.0.113.1
```

Numbering per customer NN:

| Item | Value for NN |
| --- | --- |
| VRF table | 10NN |
| VNI and VXLAN/bridge names | 100NN |
| veth | veth10NN / veth10NNo |
| IPv4 transfer net | 100.64.0.(2·NN−2)/31 |
| IPv6 transfer net | fd00:64:NN::/127 |
| NAT rule | 10NN |

Verify on VyOS:

- `show bgp vrf c01 l2vpn evpn` shows the customer's subnets as type-5 from the nodes.
- `show ip route vrf c01` holds those subnets via the VXLAN bridge.
- `show nat source translations` lists flows translated to the customer's /32.
- `show conntrack table ipv4` keeps identical flows from two overlapping customers as separate entries. If it doesn't, enable per-VRF conntrack zones before onboarding a second customer with the same range.

## Sources

- [EVPN IPv6 underlay support, applied 18 May 2026](https://lore.proxmox.com/all/177909919143.3110381.3763965075645409286.b4-ty@b4/)
- [BGP fabric series, applied](https://lore.proxmox.com/all/177909919151.3110381.4677532552380222478.b4-ty@b4/)
- [BGP fabric documentation patch (options, EVPN integration)](https://lore.proxmox.com/all/20260512141305.199664-8-h.laimer@proxmox.com/)
- [SDN fabrics documentation patch (IPv6 notes)](https://lore.proxmox.com/20250716130837.585796-77-g.goller@proxmox.com)
- [Earlier BGP controller IPv6 limitation (forum)](https://forum.proxmox.com/threads/evpn-with-ipv4-and-ipv6.170995/)
- [RouterOS EVPN documentation](https://help.mikrotik.com/docs/spaces/ROS/pages/315883568/EVPN)
