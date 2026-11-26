"""The bootstrap package's files: the unit's boot ordering, the sudo rule and the tarball."""

import configparser
import subprocess
import tarfile
from pathlib import Path

from node import build
from node import sharpwire_node as helper

NODE = Path(build.__file__).parent


def unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(NODE / "sharpwire-restore.service")
    return parser


def test_the_restore_unit_runs_before_networking_and_after_sysctl() -> None:
    # docs/05-apply-and-rollout.md#proxmox-nodes, tested through a reboot in spikes/node-linux/ (Q3).
    u = unit()
    assert u["Unit"]["DefaultDependencies"] == "no"
    assert u["Unit"]["RequiresMountsFor"] == "/var/lib/sharpwire"
    assert u["Unit"]["ConditionPathExists"] == "/var/lib/sharpwire/armed"
    assert set(u["Unit"]["After"].split()) == {
        "local-fs.target",
        "systemd-sysctl.service",
        "pvenetcommit.service",
        "systemd-journald.socket",
    }
    assert set(u["Unit"]["Before"].split()) == {
        "network-pre.target",
        "networking.service",
        "frr.service",
        "pve-sdn-commit.service",
        "pve-cluster.service",
        "corosync.service",
        "shutdown.target",
    }
    assert u["Unit"]["Conflicts"] == "shutdown.target"
    assert u["Service"]["TimeoutStartSec"] == "5min"
    assert u["Service"]["ExecStart"] == "/usr/local/sbin/sharpwire-node boot-restore"
    assert u["Install"]["WantedBy"] == "sysinit.target"


def test_the_paths_agree() -> None:
    helper_node = helper.Node()
    assert helper_node.self_path == "/usr/local/sbin/sharpwire-node"
    assert str(helper_node.marker) == unit()["Unit"]["ConditionPathExists"]
    sudoers = (NODE / "sudoers").read_text()
    assert "sharpwire ALL=(root) NOPASSWD: /usr/local/sbin/sharpwire-node\n" in sudoers
    assert "helper=/usr/local/sbin/sharpwire-node\n" in (NODE / "install.sh").read_text()


def test_install_sh_parses() -> None:
    subprocess.run(["bash", "-n", str(NODE / "install.sh")], check=True)


def test_the_tarball_is_reproducible_and_complete(tmp_path: Path) -> None:
    first = build.build(tmp_path / "a")
    second = build.build(tmp_path / "b")
    assert first.name == f"sharpwire-node-{helper.VERSION}.tar.gz"
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as tar:
        members = {m.name: m for m in tar.getmembers()}
    prefix = f"sharpwire-node-{helper.VERSION}"
    assert set(members) == {f"{prefix}/{name}" for name in build.CONTENTS}
    assert members[f"{prefix}/install.sh"].mode == 0o755
    assert members[f"{prefix}/sharpwire_node.py"].mode == 0o755
    assert all(m.uid == 0 and m.gid == 0 for m in members.values())
