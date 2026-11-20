#!/bin/sh
# Emulate pve1's NICs and the leaf/core side. Idempotent. Run before networking.service.
# netns "leaf": bond (802.3ad) over the veth peers, VLAN 10 with the mgmt gateway ::1,
# VLAN 101/102 with core A/B addresses.
set -e
ip netns add leaf 2>/dev/null || true
for i in 1 2; do
  if ! ip link show eno$i >/dev/null 2>&1; then
    ip link add eno$i type veth peer name lf$i
    ip link set lf$i netns leaf
  fi
done
L="ip -n leaf"
$L link show lbond >/dev/null 2>&1 || {
  $L link add lbond type bond mode 802.3ad lacp_rate fast miimon 100 xmit_hash_policy layer3+4
  for i in 1 2; do $L link set lf$i down; $L link set lf$i mtu 9216; $L link set lf$i master lbond; done
  $L link set lbond mtu 9216
  $L link set lbond up; $L link set lo up
  for v in 10 101 102; do $L link add link lbond name lbond.$v type vlan id $v; $L link set lbond.$v up; done
  $L addr add 2001:db8:100::1/64 dev lbond.10 nodad
  $L addr add 2001:db8:101::1/64 dev lbond.101 nodad
  $L addr add 2001:db8:102::1/64 dev lbond.102 nodad
}
