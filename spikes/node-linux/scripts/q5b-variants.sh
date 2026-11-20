#!/bin/bash
# Q5b: isolate why no-op reloads drop the session. Variants of /etc/frr/frr.conf, each loaded by restart, then reloaded.
st(){ vtysh -N leaf -c 'show bgp neighbors 2001:db8:101::11 json' | python3 -c '
import json,sys; n=list(json.load(sys.stdin).values())[0]
print("   bgp state=%s established=%s dropped=%s" % (n["bgpState"], n["connectionsEstablished"], n["connectionsDropped"]))'; }
B=/root/frr.conf.q5base; F=/etc/frr/frr.conf
run(){ echo "## variant $1"; cp /root/v.conf $F; systemctl reset-failed frr; systemctl restart frr; sleep 15; st
  echo " -- frr-reload --test lines to add/delete touching bgp/bfd:"; /usr/lib/frr/frr-reload.py --test $F 2>/dev/null | grep -E 'bfd|router-id|advertise' | sed 's/^/    /'
  echo " -- no-op reload"; /usr/lib/frr/frr-reload.py --reload $F >/dev/null 2>&1; sleep 3; st
  echo " -- unrelated route-map change + reload"; sed -i 's/^route-map PUB-OUT permit 2$/route-map PUB-OUT permit 2\n set local-preference 50/' $F
  /usr/lib/frr/frr-reload.py --reload $F >/dev/null 2>&1; sleep 3; st; }
# A: plain 'neighbor X bfd' (no inline timers, no profiles)
sed -E 's/^( neighbor \S+ bfd) [0-9]+ [0-9]+ [0-9]+$/\1/' $B > /root/v.conf; run "A plain bfd"
# B: bfd profiles defined + 'bfd profile' refs (what frr.conf would hold if PVE's merge kept the bfd block)
{ sed -E 's/^( neighbor (UL|CORE|CORE6) bfd) [0-9]+ [0-9]+ [0-9]+$/\1\n\1 profile underlay/; s/^( neighbor VTEP bfd) [0-9]+ [0-9]+ [0-9]+$/\1\n\1 profile overlay/' $B | sed '/^line vty/,$d'
  printf 'bfd\n profile underlay\n  detect-multiplier 3\n  receive-interval 300\n  transmit-interval 300\n exit\n !\n profile overlay\n  detect-multiplier 5\n  receive-interval 300\n  transmit-interval 300\n exit\n !\nexit\n!\nline vty\n!\n'; } > /root/v.conf; run "B profiles"
# C: no BFD at all on UL
sed -E '/^ neighbor UL bfd/d' $B > /root/v.conf; run "C no bfd on UL"
cp $B $F; systemctl reset-failed frr; systemctl restart frr
