#!/bin/bash
# Q5: does frr-reload.py keep unrelated sessions up? Counters read on the core side (netns leaf).
st(){ vtysh -N leaf -c 'show bgp neighbors 2001:db8:101::11 json' | python3 -c '
import json,sys; n=list(json.load(sys.stdin).values())[0]
print("bgp state=%s uptime=%s established=%s dropped=%s" % (n["bgpState"], n.get("bgpTimerUpString"), n["connectionsEstablished"], n["connectionsDropped"]))'
  vtysh -N leaf -c 'show bfd peer 2001:db8:101::11 json' | python3 -c '
import json,sys; p=json.load(sys.stdin); print("bfd status=%s uptime=%ss" % (p["status"], p.get("uptime")))'; }
F=/etc/frr/frr.conf; cp $F /root/frr.conf.q5base
echo "## baseline"; st
echo "## 1) no-op frr-reload.py --reload (file == what was loaded)"; /usr/lib/frr/frr-reload.py --reload $F >/dev/null 2>&1; echo rc=$?; sleep 3; st
echo "## 2) change unrelated route-map (PUB-OUT add entry) + reload"
sed -i 's/^route-map PUB-OUT permit 2$/route-map PUB-OUT permit 2\n set local-preference 50\nexit\n!\nroute-map PUB-OUT permit 3/' $F
/usr/lib/frr/frr-reload.py --reload $F >/dev/null 2>&1; echo rc=$?; sleep 3; vtysh -c 'show route-map PUB-OUT' | grep -c 'permit, sequence'; st
echo "## 3) change a route-map USED by the session (UL-OUT add a second loopback) + reload"
sed -i 's#^ipv6 prefix-list UL-OUT seq 10 permit 2001:db8:ffff::11/128#&\nipv6 prefix-list UL-OUT seq 20 permit 2001:db8:ffff::99/128#' $F
/usr/lib/frr/frr-reload.py --reload $F >/dev/null 2>&1; echo rc=$?; sleep 3; st
echo "## 4) router-id flip (duplicate 'bgp router-id' lines, as PVE's merge produces) + reload"
sed -i 's/^ bgp router-id 10.255.0.11$/ bgp router-id 203.0.113.17\n bgp router-id 10.255.0.11/' $F
for i in 1 2; do /usr/lib/frr/frr-reload.py --reload $F >/dev/null 2>&1; echo "reload $i rc=$?"; sleep 4; vtysh -c 'show run' | grep -m1 'bgp router-id'; st; done
cp /root/frr.conf.q5base $F; /usr/lib/frr/frr-reload.py --reload $F >/dev/null 2>&1; sleep 8
echo "## 5) systemctl restart frr (baseline)"; st; systemctl restart frr; sleep 1; st; sleep 10; st
