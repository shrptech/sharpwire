/interface ethernet
set [find name=sfp-sfpplus15] l2mtu=9500 mtu=9216
set [find name=sfp-sfpplus16] l2mtu=9500 mtu=9216
set [find name=sfp-sfpplus13] l2mtu=9500 mtu=9216
set [find name=sfp-sfpplus14] l2mtu=9500 mtu=9216
set [find name=sfp-sfpplus1] l2mtu=9500 mtu=9216
set [find name=sfp-sfpplus2] l2mtu=9500 mtu=9216
set [find name=sfp-sfpplus3] l2mtu=9500 mtu=9216
/interface bonding
add name=peer-link slaves=sfp-sfpplus15,sfp-sfpplus16 mode=802.3ad lacp-rate=1sec mtu=9216 comment=sharpwire
add name=bond-coreA slaves=sfp-sfpplus13 mode=802.3ad lacp-rate=1sec mlag-id=21 mtu=9216 comment=sharpwire
add name=bond-coreB slaves=sfp-sfpplus14 mode=802.3ad lacp-rate=1sec mlag-id=22 mtu=9216 comment=sharpwire
add name=bond-pve1 slaves=sfp-sfpplus1 mode=802.3ad lacp-rate=1sec mlag-id=11 mtu=9216 comment=sharpwire
add name=bond-pve2 slaves=sfp-sfpplus2 mode=802.3ad lacp-rate=1sec mlag-id=12 mtu=9216 comment=sharpwire
add name=bond-pve3 slaves=sfp-sfpplus3 mode=802.3ad lacp-rate=1sec mlag-id=13 mtu=9216 comment=sharpwire
/interface bridge
add name=bridge vlan-filtering=yes comment=sharpwire
/interface bridge mlag
set bridge=bridge peer-port=peer-link
/interface bridge port
add bridge=bridge interface=peer-link comment=sharpwire
add bridge=bridge interface=bond-coreA frame-types=admit-only-vlan-tagged comment=sharpwire
add bridge=bridge interface=bond-coreB frame-types=admit-only-vlan-tagged comment=sharpwire
add bridge=bridge interface=bond-pve1 frame-types=admit-only-vlan-tagged comment=sharpwire
add bridge=bridge interface=bond-pve2 frame-types=admit-only-vlan-tagged comment=sharpwire
add bridge=bridge interface=bond-pve3 frame-types=admit-only-vlan-tagged comment=sharpwire
/interface bridge vlan
add bridge=bridge vlan-ids=10 tagged=bridge,peer-link,bond-coreA,bond-coreB,bond-pve1,bond-pve2,bond-pve3 comment=sharpwire
add bridge=bridge vlan-ids=101 tagged=peer-link,bond-coreA,bond-pve1,bond-pve2,bond-pve3 comment=sharpwire
add bridge=bridge vlan-ids=102 tagged=peer-link,bond-coreB,bond-pve1,bond-pve2,bond-pve3 comment=sharpwire
add bridge=bridge vlan-ids=300 tagged=peer-link,bond-coreA,bond-coreB,bond-pve1,bond-pve2,bond-pve3 comment=sharpwire
/interface vlan
add name=mgmt interface=bridge vlan-id=10 comment=sharpwire
/ipv6 address
add address=2001:db8:100::f1/64 interface=mgmt advertise=no comment=sharpwire
