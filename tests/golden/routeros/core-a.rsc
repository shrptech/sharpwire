/interface ethernet
set [find name=sfp-sfpplus1] l2mtu=9500 mtu=9216
set [find name=sfp-sfpplus2] l2mtu=9500 mtu=9216
/interface bonding
add name=bond-leafs slaves=sfp-sfpplus1,sfp-sfpplus2 mode=802.3ad lacp-rate=1sec transmit-hash-policy=layer-3-and-4 mtu=9216 comment=sharpwire
/interface vlan
add name=vlan10 interface=bond-leafs vlan-id=10 mtu=1500 comment=sharpwire
add name=vlan101 interface=bond-leafs vlan-id=101 mtu=9216 comment=sharpwire
add name=vlan300 interface=bond-leafs vlan-id=300 mtu=9216 comment=sharpwire
/ipv6 address
add address=2001:db8:100::2/64 interface=vlan10 advertise=no comment=sharpwire
add address=2001:db8:101::1/64 interface=vlan101 advertise=no comment=sharpwire
add address=2001:db8:250::1/64 interface=vlan300 advertise=no comment=sharpwire
/ip address
add address=10.250.0.1/24 interface=vlan300 comment=sharpwire
/routing bgp instance
add name=core as=4200000001 router-id=10.255.255.1 comment=sharpwire
/routing bfd configuration
add interfaces=vlan101,vlan300 min-rx=300ms min-tx=300ms multiplier=3 comment=sharpwire
/routing filter rule
add chain=ul-in-pve1 rule="if (dst == 2001:db8:ffff::11/128) { accept }" comment=sharpwire:ul-in-pve1:accept-loopback
add chain=ul-in-pve1 rule=reject comment=sharpwire:ul-in-pve1:reject
add chain=ul-in-pve2 rule="if (dst == 2001:db8:ffff::12/128) { accept }" comment=sharpwire:ul-in-pve2:accept-loopback
add chain=ul-in-pve2 rule=reject comment=sharpwire:ul-in-pve2:reject
add chain=ul-in-pve3 rule="if (dst == 2001:db8:ffff::13/128) { accept }" comment=sharpwire:ul-in-pve3:accept-loopback
add chain=ul-in-pve3 rule=reject comment=sharpwire:ul-in-pve3:reject
add chain=ul-out rule="if (dst in 2001:db8:ffff::/64 && dst-len == 128) { accept }" comment=sharpwire:ul-out:accept-vteps
add chain=ul-out rule=reject comment=sharpwire:ul-out:reject
add chain=pub-in rule="if (dst in 198.51.100.0/24 && dst-len in 24-28) { accept }" comment=sharpwire:pub-in:accept-198.51.100.0/24
add chain=pub-in rule="if (dst in 2001:db8:70::/48 && dst-len in 48-64) { accept }" comment=sharpwire:pub-in:accept-2001:db8:70::/48
add chain=pub-in rule=reject comment=sharpwire:pub-in:reject
add chain=default-out rule="if (dst == 0.0.0.0/0 || dst == ::/0) { accept }" comment=sharpwire:default-out:accept-defaults
add chain=default-out rule=reject comment=sharpwire:default-out:reject
/routing bgp connection
add name=ul-pve1 instance=core afi=ipv6 local.role=ebgp local.address=2001:db8:101::1 remote.address=2001:db8:101::11 remote.as=4200000011 use-bfd=yes nexthop-choice=propagate input.filter=ul-in-pve1 output.filter-chain=ul-out comment=sharpwire
add name=ul-pve2 instance=core afi=ipv6 local.role=ebgp local.address=2001:db8:101::1 remote.address=2001:db8:101::12 remote.as=4200000012 use-bfd=yes nexthop-choice=propagate input.filter=ul-in-pve2 output.filter-chain=ul-out comment=sharpwire
add name=ul-pve3 instance=core afi=ipv6 local.role=ebgp local.address=2001:db8:101::1 remote.address=2001:db8:101::13 remote.as=4200000013 use-bfd=yes nexthop-choice=propagate input.filter=ul-in-pve3 output.filter-chain=ul-out comment=sharpwire
add name=pub4 instance=core afi=ip local.role=ebgp local.address=10.250.0.1 remote.address=10.250.0.0/24 remote.as=4200000100 use-bfd=yes input.filter=pub-in output.filter-chain=default-out output.default-originate=always comment=sharpwire
add name=pub6 instance=core afi=ipv6 local.role=ebgp local.address=2001:db8:250::1 remote.address=2001:db8:250::/64 remote.as=4200000100 use-bfd=yes input.filter=pub-in output.filter-chain=default-out output.default-originate=always comment=sharpwire
