#!/usr/bin/perl
# Run PVE 9's real append_local_config() (libpve-network-perl, PVE/Network/SDN/Frr.pm)
# on a hand-built "generated" EVPN config (shape copied from Controllers/EvpnPlugin.pm
# for: peers-only EVPN controller asn 65000, zone "public" vrf-vxlan 20000,
# advertise-subnets 1, no exit nodes) and dump {frr} as JSON for render.py.
# usage: perl -I stubs -I <pve-network>/usr/share/perl5 merge.pl frr.conf.local > merged.json
use strict; use warnings; use JSON::PP;
use PVE::Network::SDN::Frr;
my $local = PVE::Tools::file_get_contents($ARGV[0]);
my $rid = $ENV{GEN_ROUTER_ID} // '203.0.113.17';   # MAC-derived, as get_router_id() does for IPv6
my $cfg = { frr => {
  bgp => {
    vrf_router => {
      default => { asn => 65000, router_id => $rid, default_ipv4_unicast => JSON::PP::false,
        hard_administrative_reset => JSON::PP::false, graceful_restart_notification => JSON::PP::false,
        coalesce_time => 1000,
        neighbor_groups => [ { name => 'VTEP', bfd => 1, remote_as => 65000, interfaces => [],
          ips => [qw(2001:db8:ffff::12 2001:db8:ffff::13 2001:db8:ffff::101 2001:db8:ffff::102)] } ],
        address_families => { l2vpn_evpn => { advertise_all_vni => 1,
          neighbors => [ { name => 'VTEP', route_map_in => 'MAP_VTEP_IN', route_map_out => 'MAP_VTEP_OUT' } ] } } },
      vrf_public => { asn => 65000, router_id => $rid, hard_administrative_reset => JSON::PP::false,
        graceful_restart_notification => JSON::PP::false, neighbor_groups => [],
        address_families => {
          ipv4_unicast => { redistribute => [ { protocol => 'connected' } ] },
          ipv6_unicast => { redistribute => [ { protocol => 'connected' } ] },
          l2vpn_evpn => { advertise_ipv4_unicast => 1, advertise_ipv6_unicast => 1 } } },
    },
    vrfs => { vrf_public => { vni => 20000, ip_routes => [] } },
  },
  routemaps => { MAP_VTEP_IN => [ { seq => 1, action => 'permit' } ],
                 MAP_VTEP_OUT => [ { seq => 1, action => 'permit' } ] },
} };
if (($ENV{ADVERTISE_SUBNETS} // 1) == 0) {   # zone public with advertise-subnets 0
  $cfg->{frr}{bgp}{vrf_router}{vrf_public}{address_families} = {};
}
PVE::Network::SDN::Frr::append_local_config($cfg, $local);
my $dropped = $cfg->{''};  # where lines outside any recognised block end up
print JSON::PP->new->canonical->pretty->encode({ frr => $cfg->{frr}, dropped => $dropped // [] });
