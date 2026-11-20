"""Load the network doc's example network (docs/network/proxmox-evpn-ipv6-underlay.md, phase 1) into a NetBox.

For the dev NetBox on the test config, and the source of the controller's GraphQL snapshot
(tests/fixtures/netbox/). Run it inside NetBox, on an empty database (it refuses otherwise):

    NETBOX_ROOT=$PWD/testing uv run netbox shell --command "exec(open('testing/seed_example_network.py').read())"

Every fabric object is tagged sharpwire-managed, as the intent builder expects (docs/09-sharpstack-integration.md).
It also creates a superuser `admin` (password `admin`) and an API token for the controller, printed at the end.
These are dev-only credentials, so it refuses a NetBox without DEVELOPER = True.
"""

from dcim.models import Cable, Device, DeviceRole, DeviceType, Interface, Manufacturer, Platform, Site
from django.conf import settings
from django.db import transaction
from extras.models import Tag
from ipam.models import ASN, RIR, VLAN, VRF, IPAddress, Prefix, Role
from sharpwire_netbox.models import VNet, Zone
from users.models import Token, User
from virtualization.models import Cluster, ClusterType

if not settings.DEVELOPER:
    raise SystemExit("This seed creates admin/admin. Run it only on a dev NetBox (DEVELOPER = True).")
if Device.objects.exists():
    raise SystemExit("This NetBox already has devices. Seed an empty one.")

MGMT, UL_A, UL_B, PUB = 10, 101, 102, 300
FABRIC_VLANS = (MGMT, UL_A, UL_B, PUB)
SFP = "10gbase-x-sfpp"


def role(slug):
    return Role.objects.get(slug=slug)


def tag(slug):
    return Tag.objects.get(slug=slug)


def managed(obj):
    """Tag a fabric object sharpwire-managed, so the intent builder reads it."""
    obj.tags.add(tag("sharpwire-managed"))
    return obj


with transaction.atomic():
    site = Site.objects.create(name="lab", slug="lab")
    rir = RIR.objects.create(name="Private", slug="private", is_private=True)
    vrf_public = VRF.objects.create(name="vrf_public")

    def asn(number, *tags):
        obj = ASN.objects.create(asn=number, rir=rir)
        obj.tags.set([tag(t) for t in (*tags, "sharpwire-managed")])
        return obj

    asn(65000, "overlay")
    asn(4200000100, "public-handoff")

    vlans = {
        MGMT: managed(VLAN.objects.create(vid=MGMT, name="mgmt", role=role("mgmt"))),
        UL_A: managed(VLAN.objects.create(vid=UL_A, name="underlay-a", role=role("underlay"))),
        UL_B: managed(VLAN.objects.create(vid=UL_B, name="underlay-b", role=role("underlay"))),
        PUB: managed(VLAN.objects.create(vid=PUB, name="public-handoff", role=role("public-handoff"))),
    }

    def prefix(cidr, **kwargs):
        return managed(Prefix.objects.create(prefix=cidr, **kwargs))

    prefix("2001:db8:100::/64", vlan=vlans[MGMT])
    prefix("2001:db8:101::/64", vlan=vlans[UL_A])
    prefix("2001:db8:102::/64", vlan=vlans[UL_B])
    prefix("10.250.0.0/24", vlan=vlans[PUB])
    prefix("2001:db8:250::/64", vlan=vlans[PUB])
    prefix("2001:db8:ffff::/64", role=role("vtep"))
    prefix("198.51.100.0/24", role=role("public"), custom_field_data={"max_subnet_length": 28})
    prefix("2001:db8:70::/48", role=role("public"), custom_field_data={"max_subnet_length": 64})

    mikrotik = Manufacturer.objects.create(name="MikroTik", slug="mikrotik")
    generic = Manufacturer.objects.create(name="Generic", slug="generic")
    ccr = DeviceType.objects.create(manufacturer=mikrotik, model="CCR2116-12G-4S+", slug="ccr2116")
    crs = DeviceType.objects.create(manufacturer=mikrotik, model="CRS317-1G-16S+", slug="crs317")
    server = DeviceType.objects.create(manufacturer=generic, model="Server", slug="server")
    cluster = Cluster.objects.create(
        name="pve",
        type=ClusterType.objects.get_or_create(name="Proxmox VE", slug="proxmox-ve")[0],
        custom_field_data={"firewall_enabled": True},
    )

    def device(name, kind, device_type, platform, *, asn_number=None, router_id=None, **extra):
        cf = {"sharpwire_managed": True}
        if asn_number is not None:
            cf["asn"] = asn(asn_number).pk
        if router_id is not None:
            cf["router_id"] = router_id
        return managed(
            Device.objects.create(
                name=name,
                role=DeviceRole.objects.get(slug=kind),
                device_type=device_type,
                platform=Platform.objects.get(slug=platform),
                site=site,
                custom_field_data=cf,
                **extra,
            )
        )

    def iface(dev, name, type_, **kwargs):
        tagged = kwargs.pop("tagged", None)
        addresses = kwargs.pop("addresses", ())
        obj = Interface.objects.create(device=dev, name=name, type=type_, **kwargs)
        if tagged:
            obj.tagged_vlans.set([vlans[v] for v in tagged])
        for address in addresses:
            IPAddress.objects.create(address=address, assigned_object=obj, vrf=kwargs.get("vrf"))
        return obj

    def port(dev, name, **kwargs):
        return iface(dev, name, SFP, mtu=9216, custom_field_data={"l2mtu": 9500}, **kwargs)

    def vlan_iface(dev, name, vid, parent, mtu, *addresses, vrf=None):
        return iface(
            dev, name, "virtual", parent=parent, mode="access", untagged_vlan=vlans[vid], mtu=mtu,
            addresses=addresses, vrf=vrf,
        )  # fmt: skip

    def cable(a, b):
        Cable.objects.create(a_terminations=[a], b_terminations=[b])

    # Leafs: the CRS317 MLAG pair (network doc Appendix A.1).
    leaf_ports = {}
    for x, suffix in (("a", "f1"), ("b", "f2")):
        leaf = device(f"leaf-{x}", "leaf", crs, "routeros")
        bridge = iface(leaf, "bridge", "bridge")
        bonds = {
            "peer-link": (None, (15, 16), FABRIC_VLANS),
            **{f"bond-pve{i}": (10 + i, (i,), FABRIC_VLANS) for i in (1, 2, 3)},
            "bond-coreA": (21, (13,), (MGMT, UL_A, PUB)),
            "bond-coreB": (22, (14,), (MGMT, UL_B, PUB)),
        }
        for name, (mlag_id, ports, tagged) in bonds.items():
            lag = iface(
                leaf, name, "lag", mtu=9216, mode="tagged", tagged=tagged,
                custom_field_data={"mlag_id": mlag_id} if mlag_id else {},
            )  # fmt: skip
            for n in ports:
                leaf_ports[(x, n)] = port(leaf, f"sfp-sfpplus{n}", lag=lag)
        iface(
            leaf, "mgmt", "virtual", parent=bridge, mode="access", untagged_vlan=vlans[MGMT],
            addresses=(f"2001:db8:100::{suffix}/64",),
        )  # fmt: skip
    for n in (15, 16):
        cable(leaf_ports[("a", n)], leaf_ports[("b", n)])

    # Cores: CCR2116 (network doc Appendix A.2 and A.3).
    cores = (
        ("core-a", 4200000001, "10.255.255.1", "2001:db8:100::2/64", UL_A, "2001:db8:101::1/64", 13, 1),
        ("core-b", 4200000002, "10.255.255.2", "2001:db8:100::3/64", UL_B, "2001:db8:102::1/64", 14, 2),
    )
    for name, number, rid, mgmt, ul, ul_address, leaf_port, host in cores:
        core = device(name, "core", ccr, "routeros", asn_number=number, router_id=rid)
        lag = iface(core, "bond-leafs", "lag", mtu=9216, mode="tagged", tagged=(MGMT, ul, PUB))
        p1 = port(core, "sfp-sfpplus1", lag=lag)
        p2 = port(core, "sfp-sfpplus2", lag=lag)
        cable(p1, leaf_ports[("a", leaf_port)])
        cable(p2, leaf_ports[("b", leaf_port)])
        vlan_iface(core, f"vlan{MGMT}", MGMT, lag, 1500, mgmt)
        vlan_iface(core, f"vlan{ul}", ul, lag, 9216, ul_address)
        vlan_iface(core, f"vlan{PUB}", PUB, lag, 9216, f"10.250.0.{host}/24", f"2001:db8:250::{host}/64")

    # Nodes: network doc Steps 2 and 5c.
    for i in (1, 2, 3):
        h = 10 + i
        node = device(
            f"pve{i}", "pve-node", server, "proxmox", asn_number=4200000010 + i, router_id=f"10.255.0.{h}",
            cluster=cluster,
        )  # fmt: skip
        bond = iface(node, "bond0", "lag", mtu=9216, mode="tagged", tagged=FABRIC_VLANS)
        eno1 = iface(node, "eno1", SFP, mtu=9216, lag=bond)
        eno2 = iface(node, "eno2", SFP, mtu=9216, lag=bond)
        cable(eno1, leaf_ports[("a", i)])
        cable(eno2, leaf_ports[("b", i)])
        vlan_iface(node, f"bond0.{MGMT}", MGMT, bond, 1500, f"2001:db8:100::{h}/64")
        vlan_iface(node, f"bond0.{UL_A}", UL_A, bond, 9216, f"2001:db8:101::{h}/64")
        vlan_iface(node, f"bond0.{UL_B}", UL_B, bond, 9216, f"2001:db8:102::{h}/64")
        vlan_iface(node, f"bond0.{PUB}", PUB, bond, 9216, f"10.250.0.{h}/24", f"2001:db8:250::{h}/64", vrf=vrf_public)
        iface(node, "dummy0", "virtual", addresses=(f"2001:db8:ffff::{h}/128",))

    # The public zone and its first VNet (network doc Step 5c).
    zone = Zone.objects.create(kind="public", vrf=vrf_public, l3vni=20000, mtu=1500)
    vnet = VNet.objects.create(zone=zone, vni=200)
    subnets = [
        Prefix.objects.create(prefix="198.51.100.0/26", vrf=vrf_public),
        Prefix.objects.create(prefix="2001:db8:70:200::/64", vrf=vrf_public),
    ]
    vnet.subnets.set(subnets)
    IPAddress.objects.create(address="198.51.100.1/26", vrf=vrf_public, role="anycast")
    IPAddress.objects.create(address="2001:db8:70:200::1/64", vrf=vrf_public, role="anycast")

    admin = User.objects.create_superuser("admin", password="admin")
    token = Token.objects.create(user=admin, description="sharpwire controller (dev)")

print(f"Seeded. NETBOX_TOKEN=nbt_{token.key}.{token.token}")
