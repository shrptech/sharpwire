#!/bin/sh
# Runs inside the lab container: FRR peers in netns on br-seg (same L2 as core ether2).
#  a,b: underlay-like peers on 2001:db8:101::/64 (own ASNs, explicit connections on core)  -> Q6
#  c,d: public-handoff-like peers on 2001:db8:250::/64 (shared AS 4200000100, core listener) -> Q4
set -e
peer() { # name addr asn announce
  n=$1; ip netns add ns-$n 2>/dev/null || true
  ip link add v-$n type veth peer name e0 netns ns-$n 2>/dev/null || true
  ip link set v-$n master br-seg up
  ip netns exec ns-$n sh -c "ip link set lo up; ip link set e0 up; ip -6 addr add $2/64 dev e0 nodad 2>/dev/null; ip -6 addr add $4 dev lo 2>/dev/null || true"
  mkdir -p /run/frr-$n /etc/frr/$n; chown -R frr:frr /run/frr-$n /etc/frr/$n 2>/dev/null || true
  pfx=$4; nb=$5
  cat > /etc/frr/$n/frr.conf <<C
frr defaults traditional
hostname $n
router bgp $3
 bgp router-id 10.0.0.$6
 no bgp ebgp-requires-policy
 no bgp default ipv4-unicast
 neighbor $nb remote-as external
 address-family ipv6 unicast
  network $pfx
  neighbor $nb activate
 exit-address-family
C
  touch /etc/frr/$n/vtysh.conf; chown -R frr:frr /etc/frr/$n
  ip netns exec ns-$n /usr/lib/frr/mgmtd -d -N $n -F traditional 2>/dev/null || true
  ip netns exec ns-$n /usr/lib/frr/zebra -d -N $n -F traditional
  ip netns exec ns-$n /usr/lib/frr/bgpd -d -N $n -F traditional -f /etc/frr/$n/frr.conf
}
peer a 2001:db8:101::11 4200000011 2001:db8:ffff::11/128 2001:db8:101::1 11
peer b 2001:db8:101::12 4200000012 2001:db8:ffff::12/128 2001:db8:101::1 12
peer c 2001:db8:250::11 4200000100 2001:db8:70:11::/64   2001:db8:250::1 21
peer d 2001:db8:250::12 4200000100 2001:db8:70:12::/64   2001:db8:250::1 22
# BFD: RouterOS 7.25 with use-bfd=yes refuses to bring BGP up until BFD is up -> run bfdd + neighbor bfd.
if [ "$BFD" = 1 ]; then
  for n in a b c d; do
    ip netns exec ns-$n /usr/lib/frr/bfdd -d -N $n -F traditional
    nb=$(grep -o 'neighbor [^ ]* remote-as' /etc/frr/$n/frr.conf | awk '{print $2}'); asn=$(awk '/^router bgp/{print $3}' /etc/frr/$n/frr.conf)
    vtysh -N $n -c "conf t" -c "bfd" -c "peer $nb" -c "receive-interval 300" -c "transmit-interval 300" -c "exit" -c "exit" -c "router bgp $asn" -c "neighbor $nb bfd"
  done
fi
