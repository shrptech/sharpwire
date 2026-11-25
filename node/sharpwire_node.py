#!/usr/bin/python3 -I
"""sharpwire-node: the root helper the controller runs on each Proxmox node, through sudo.

It reads the managed host files and the live state the drift check needs, and it owns the node's
armed rollback (docs/05-apply-and-rollout.md#proxmox-nodes). It holds no rules: what a file should
contain, and whether the node drifted, is decided by the controller from what `read` returns.

Every command prints one JSON object on stdout. Failures exit 1 and print
`{"detail": ..., "error_code": ...}`, the same shape as the controller's API errors.

It runs on the python3 that Proxmox VE 9 ships (Debian 13, Python 3.13), with the stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import syslog
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, Protocol

# Bumped on every change the controller can tell apart. The controller refuses to plan for a node
# whose helper is older than the version it supports.
VERSION = 1

# The managed scope (docs/04-drivers.md#node-targets-host-files). Nothing else is read or written.
MANAGED_FILES = (
    "/etc/network/interfaces",
    "/etc/sysctl.d/90-sdn.conf",
    "/etc/frr/frr.conf.local",
    "/etc/hosts",
)
# FRR boots from the generated file and nothing can regenerate it early at boot, so it's
# snapshotted with the managed files. It's never read for the diff.
GENERATED_FRR_CONF = "/etc/frr/frr.conf"
SNAPSHOT_FILES = (*MANAGED_FILES, GENERATED_FRR_CONF)
INTERFACES_NEW = "/etc/network/interfaces.new"

RESTORE_SERVICE = "sharpwire-restore.service"
# Starts with a letter or digit, so it can never be read as an option.
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# The docs' arm window is 10 minutes; anything outside this range is a controller bug.
ARM_SECONDS = range(60, 3601)
# Snapshots kept besides the armed one; older ones are pruned when a new one is taken.
KEEP_SNAPSHOTS = 20
# How long the controller's commands wait for the lock. A restore never gives up waiting.
LOCK_WAIT_SECONDS = 300
COMMAND_TIMEOUT_SECONDS = 120
# ifreload with many vnets and LACP waits, or an FRR restart, can be slow. Killing one half-applied
# is worse than waiting.
RELOAD_TIMEOUT_SECONDS = 900
# The transient restore unit retries until it succeeds, so a failed restore isn't single-shot.
RESTORE_RETRY_SECONDS = 30

IFQUERY = "/usr/sbin/ifquery"
IFRELOAD = "/usr/sbin/ifreload"
IP = "/usr/sbin/ip"
SYSCTL = "/usr/sbin/sysctl"
SYSTEMD_SYSCTL = "/usr/lib/systemd/systemd-sysctl"
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
VTYSH = "/usr/bin/vtysh"
FRR_RELOAD = "/usr/lib/frr/frr-reload.py"

# Live state for the drift check and the post-merge check, returned raw for the controller to parse.
# ifquery --check alone misses a deleted default route, interfaces outside the file and removed
# stanzas (spikes/node-linux/FINDINGS.md, Q1), hence the ip views next to it.
LIVE_COMMANDS: dict[str, tuple[str, ...]] = {
    "ifquery": (IFQUERY, "--all", "--format", "json"),
    "ifquery_check": (IFQUERY, "--check", "--all", "--format", "json"),
    "ip_link": (IP, "-json", "-details", "link", "show"),
    "ip_addr": (IP, "-json", "addr", "show"),
    "ip_route_default_v4": (IP, "-json", "-4", "route", "show", "default"),
    "ip_route_default_v6": (IP, "-json", "-6", "route", "show", "default"),
    "frr_running_config": (VTYSH, "-c", "show running-config"),
    "frr_bgp_summary": (VTYSH, "-c", "show bgp vrf all summary json"),
    "frr_bfd_peers": (VTYSH, "-c", "show bfd peers json"),
}


class HelperError(Exception):
    def __init__(self, detail: str, error_code: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.error_code = error_code


@dataclass(frozen=True)
class Result:
    rc: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: int = ...) -> Result: ...


def run_command(argv: Sequence[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> Result:
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{argv[0]}: timed out after {timeout}s")
    except OSError as exc:
        return Result(127, "", f"{argv[0]}: {exc.strerror}")
    return Result(proc.returncode, proc.stdout, proc.stderr)


def log(message: str) -> None:
    syslog.openlog("sharpwire-node", 0, syslog.LOG_DAEMON)
    syslog.syslog(syslog.LOG_INFO, message)


@dataclass(frozen=True)
class Node:
    """The node's filesystem and command runner. `root` is "/" on a node; tests point it elsewhere."""

    root: Path = Path("/")
    run: Runner = run_command
    log: Callable[[str], None] = log
    self_path: str = "/usr/local/sbin/sharpwire-node"
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    @property
    def state(self) -> Path:
        return self.path("/var/lib/sharpwire")

    @property
    def marker(self) -> Path:
        return self.state / "armed"

    def run_dir(self, run_id: str) -> Path:
        return self.state / "rollback" / run_id


# ---------------------------------------------------------------------------------------------
# Files. /etc/frr is writable by the frr user, so nothing here follows a symlink: root never
# reads or writes through a link someone else planted.


class NotARegularFile(Exception):
    pass


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_regular(path: Path) -> tuple[bytes, os.stat_result] | None:
    """The file's bytes and stat, or None when it doesn't exist. Refuses symlinks and non-files."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # ELOOP: the path is a symlink.
        raise NotARegularFile(str(path)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotARegularFile(str(path))
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks), st
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes, *, mode: int, uid: int, gid: int) -> None:
    """Replace `path` so a crash leaves either the old file or the new one, never a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.sharpwire-tmp")
    # Whatever is at the temp name, a planted symlink included, goes first; O_EXCL|O_NOFOLLOW then
    # fails rather than write through anything that appears in between.
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(path.parent)


def write_own(path: Path, data: bytes) -> None:
    """A file of the helper's own state: root's, mode 0600."""
    write_atomic(path, data, mode=0o600, uid=os.getuid(), gid=os.getgid())


def remove_durably(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    fsync_dir(path.parent)
    return True


@contextlib.contextmanager
def locked(node: Node, *, forever: bool = False) -> Iterator[None]:
    """Serialise every command that touches the rollback state. It waits LOCK_WAIT_SECONDS, or `forever`.

    The timer's restore and the controller's disarm can run at the same moment. Whoever takes the lock
    second sees what the first did: a disarm after a restore reports that the rollback fired, and a
    restore after a disarm does nothing.
    """
    lock_path = node.path("/run/sharpwire-node.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = None if forever else time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() > deadline:
                    raise HelperError(
                        "Another sharpwire-node command is still running. Try again later.", "busy"
                    ) from None
                time.sleep(0.5)
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------------------------
# Rollback state


def check_run_id(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise HelperError(
            "A run ID is 1 to 64 letters, digits, '-' or '_', starting with a letter or digit.", "invalid_run_id"
        )
    return run_id


def armed_run(node: Node) -> str | None:
    try:
        return node.marker.read_text(encoding="utf-8", errors="replace").strip() or None
    except FileNotFoundError:
        return None


def timer_unit(run_id: str) -> str:
    return f"sharpwire-rollback-{run_id}"


def unit_active(node: Node, unit: str) -> bool:
    return node.run((SYSTEMCTL, "is-active", "--quiet", unit)).rc == 0


def restore_record(node: Node, run_id: str) -> dict[str, Any] | None:
    try:
        record: dict[str, Any] = json.loads((node.run_dir(run_id) / "restored.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return record


def load_snapshot(node: Node, run_id: str) -> dict[str, Any]:
    """The run's manifest, once every file it lists is known to be there and complete.

    Checked before arming and before restoring, so a restore never writes half a snapshot because a
    copy turned out to be missing.
    """
    run_dir = node.run_dir(run_id)
    try:
        manifest: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HelperError(f"Run {run_id} has no snapshot. Take one first.", "no_snapshot") from None
    except (OSError, ValueError) as exc:
        raise HelperError(f"Run {run_id}'s snapshot manifest is unreadable.", "snapshot_damaged") from exc
    try:
        for entry in manifest["files"]:
            if not entry["present"]:
                continue
            if not all(isinstance(entry[key], int) for key in ("size", "mode", "uid", "gid")):
                raise ValueError(entry["path"])
            found = read_regular(run_dir / "files" / entry["path"].lstrip("/"))
            if found is None or len(found[0]) != entry["size"]:
                raise HelperError(
                    f"Run {run_id}'s copy of {entry['path']} is missing or incomplete.", "snapshot_damaged"
                )
    except (KeyError, TypeError, ValueError, NotARegularFile) as exc:
        raise HelperError(f"Run {run_id}'s snapshot manifest is damaged.", "snapshot_damaged") from exc
    return manifest


def prune_snapshots(node: Node, keep: set[str]) -> None:
    rollback = node.state / "rollback"
    runs = sorted(
        (p for p in rollback.iterdir() if p.is_dir() and not p.is_symlink() and p.name not in keep),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in runs[KEEP_SNAPSHOTS:]:
        shutil.rmtree(old)
        node.log(f"pruned snapshot {old.name}")


def cmd_snapshot(node: Node, run_id: str) -> dict[str, Any]:
    """Copy the snapshot files, with owner and mode, to /var/lib/sharpwire/rollback/<run>/."""
    check_run_id(run_id)
    with locked(node):
        if (armed := armed_run(node)) is not None:
            raise HelperError(f"Run {armed} is armed. Disarm it before taking a new snapshot.", "already_armed")
        run_dir = node.run_dir(run_id)
        if run_dir.exists():
            raise HelperError(f"Run {run_id} already has a snapshot. Use a new run ID.", "snapshot_exists")
        node.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.mkdir(mode=0o700, parents=True)
        files: list[dict[str, Any]] = []
        for name in SNAPSHOT_FILES:
            try:
                found = read_regular(node.path(name))
            except NotARegularFile:
                shutil.rmtree(run_dir)
                raise HelperError(
                    f"{name} is a symlink or not a regular file. Fix it by hand before applying.", "not_a_file"
                ) from None
            if found is None:
                files.append({"path": name, "present": False})
                continue
            data, st = found
            write_own(run_dir / "files" / name.lstrip("/"), data)
            files.append(
                {
                    "path": name,
                    "present": True,
                    "size": len(data),
                    "mode": stat.S_IMODE(st.st_mode),
                    "uid": st.st_uid,
                    "gid": st.st_gid,
                }
            )
        manifest = {"run": run_id, "taken_at": node.now().isoformat(), "helper_version": VERSION, "files": files}
        # Every copy and directory reaches the disk before the manifest that vouches for them.
        os.sync()
        write_own(run_dir / "manifest.json", json.dumps(manifest, indent=2).encode())
        fsync_dir(run_dir.parent)
        prune_snapshots(node, keep={run_id})
    node.log(f"snapshot {run_id}: {', '.join(f['path'] for f in files if f['present'])}")
    return {"run": run_id, "files": files}


def cmd_arm(node: Node, run_id: str, seconds: int) -> dict[str, Any]:
    """Write the marker durably, then start a transient timer that restores at T+seconds.

    The marker comes first: if the node reboots before the timer exists, sharpwire-restore.service
    still restores at boot. A marker without a timer (the helper killed in between) shows in `status`
    as armed with no timer active.
    """
    check_run_id(run_id)
    if seconds not in ARM_SECONDS:
        raise HelperError(
            f"The rollback must fire {ARM_SECONDS.start} to {ARM_SECONDS.stop - 1} s from now.", "invalid_delay"
        )
    with locked(node):
        if (armed := armed_run(node)) is not None:
            raise HelperError(f"Run {armed} is already armed. Disarm it first.", "already_armed")
        load_snapshot(node, run_id)
        if restore_record(node, run_id) is not None:
            raise HelperError(f"Run {run_id} was already restored. Use a new run ID.", "rollback_fired")
        if node.run((SYSTEMCTL, "is-enabled", "--quiet", RESTORE_SERVICE)).rc != 0:
            raise HelperError(
                f"{RESTORE_SERVICE} isn't enabled. Re-run the bootstrap package's install.sh.", "not_installed"
            )

        write_own(node.marker, f"{run_id}\n".encode())
        started = node.run(
            (
                SYSTEMD_RUN,
                "--quiet",
                "--collect",
                f"--on-active={seconds}s",
                "--timer-property=AccuracySec=1s",
                f"--unit={timer_unit(run_id)}",
                # A restore that fails (exit 1, marker kept) runs again until it succeeds.
                "--property=Restart=on-failure",
                f"--property=RestartSec={RESTORE_RETRY_SECONDS}s",
                "--property=StartLimitIntervalSec=0",
                "--",
                node.self_path,
                "restore",
                "--trigger",
                "timer",
                "--",
                run_id,
            )
        )
        if started.rc != 0:
            remove_durably(node.marker)
            node.log(f"arm {run_id}: systemd-run failed: {started.stderr.strip()}")
            raise HelperError(
                "The rollback timer couldn't be started, so nothing was armed. See the node's journal.", "arm_failed"
            )
    node.log(f"armed {run_id}: restores in {seconds}s unless disarmed")
    return {"run": run_id, "armed": True, "fires_in_seconds": seconds}


def cmd_disarm(node: Node, run_id: str) -> dict[str, Any]:
    """Stop the timer, then remove the marker. Fails if the rollback already fired.

    It waits for a running restore however long that takes, so it never reports `busy` for a run whose
    rollback is firing.
    """
    check_run_id(run_id)
    with locked(node, forever=True):
        armed = armed_run(node)
        if armed != run_id:
            if (record := restore_record(node, run_id)) is not None:
                raise HelperError(
                    f"Run {run_id}'s rollback already fired ({record['trigger']}, {record['at']}). "
                    "The node is back on its snapshot.",
                    "rollback_fired",
                )
            raise HelperError(f"Run {run_id} isn't armed.", "not_armed")
        # A timer that already fired has no unit left to stop; that's fine, the restore it started is
        # waiting for this lock and will see the marker gone.
        node.run((SYSTEMCTL, "stop", f"{timer_unit(run_id)}.timer"))
        remove_durably(node.marker)
    node.log(f"disarmed {run_id}")
    return {"run": run_id, "armed": False}


def put_back(node: Node, run_id: str, manifest: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Restore every file it can, one at a time, so one failure doesn't stop the rest."""
    changed: list[str] = []
    errors: dict[str, str] = {}
    for entry in manifest["files"]:
        name = entry["path"]
        target = node.path(name)
        try:
            if entry["present"]:
                found = read_regular(node.run_dir(run_id) / "files" / name.lstrip("/"))
                if found is None:
                    raise FileNotFoundError(name)
                write_atomic(target, found[0], mode=entry["mode"], uid=entry["uid"], gid=entry["gid"])
                changed.append(name)
            elif remove_durably(target):
                changed.append(name)
        except (OSError, NotARegularFile) as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
    return changed, errors


def step(node: Node, argv: Sequence[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> int:
    result = node.run(argv, timeout=timeout)
    if result.rc != 0:
        node.log(f"{' '.join(argv)}: exit {result.rc}: {result.stderr.strip()[-2000:]}")
    return result.rc


def cmd_restore(node: Node, run_id: str, trigger: str) -> dict[str, Any]:
    """Put run's snapshot back. At boot (`boot-restore`) it doesn't reload: the services that follow
    it start from the restored files. Otherwise it reloads networking, sysctl and FRR the way PVE does.

    The marker stays until every file is back, so a restore that couldn't write a file fails, and is
    retried by the timer unit and again at boot. A reload step that fails is recorded but doesn't keep
    the marker: the files are right, and the next boot starts from them anyway.
    """
    check_run_id(run_id)
    # The timer and boot restores wait however long it takes: giving up would strand the node.
    with locked(node, forever=trigger in ("timer", "boot")):
        if armed_run(node) != run_id:
            if trigger == "timer":
                # Disarmed while this was waiting to start.
                node.log(f"restore {run_id}: not armed any more, nothing to do")
                return {"run": run_id, "restored": False}
            if restore_record(node, run_id) is not None:
                raise HelperError(f"Run {run_id} was already restored.", "rollback_fired")
            raise HelperError(f"Run {run_id} isn't armed.", "not_armed")

        manifest = load_snapshot(node, run_id)
        if trigger == "controller":
            # The timer would find nothing to do, but needn't linger.
            node.run((SYSTEMCTL, "stop", f"{timer_unit(run_id)}.timer"))
        node.log(f"restore {run_id} ({trigger}): putting the snapshot back")
        files, errors = put_back(node, run_id, manifest)
        # A pending interfaces.new would be promoted over the restored file by the next PVE reload.
        try:
            if remove_durably(node.path(INTERFACES_NEW)):
                files.append(INTERFACES_NEW)
        except OSError as exc:
            errors[INTERFACES_NEW] = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(OSError):
            os.sync()
        # ifupdown2 fails with "another instance is running" without it.
        with contextlib.suppress(OSError):
            node.path("/run/network").mkdir(parents=True, exist_ok=True)

        steps: dict[str, int] = {}
        if trigger == "boot":
            # systemd-sysctl.service already applied the broken file before this ran.
            steps["systemd_sysctl"] = step(node, (SYSTEMD_SYSCTL,))
        else:
            steps["ifreload"] = step(node, (IFRELOAD, "-a"), timeout=RELOAD_TIMEOUT_SECONDS)
            steps["sysctl"] = step(node, (SYSCTL, "--quiet", "--system"))
            if unit_active(node, "frr.service"):
                # PVE's own per-node step: reload, falling back to a restart.
                steps["frr_reload"] = step(
                    node, (FRR_RELOAD, "--reload", GENERATED_FRR_CONF), timeout=RELOAD_TIMEOUT_SECONDS
                )
                if steps["frr_reload"] != 0:
                    steps["frr_restart"] = step(
                        node, (SYSTEMCTL, "restart", "frr.service"), timeout=RELOAD_TIMEOUT_SECONDS
                    )

        record = {
            "run": run_id,
            "trigger": trigger,
            "at": node.now().isoformat(),
            "complete": not errors,
            "files": files,
            "errors": errors,
            "steps": steps,
        }
        record_path = node.run_dir(run_id) / ("restored.json" if not errors else "restore-failed.json")
        with contextlib.suppress(OSError):
            write_own(record_path, json.dumps(record, indent=2).encode())
        if errors:
            node.log(f"restore {run_id} ({trigger}) incomplete, still armed: {errors}")
            raise HelperError(
                f"Run {run_id}'s restore couldn't put back {', '.join(errors)}. It stays armed and is retried.",
                "restore_incomplete",
            )
        # Last, so a crash mid-restore restores again at boot.
        remove_durably(node.marker)
    failed = [name for name, rc in steps.items() if rc != 0]
    summary = f"{', '.join(files) or 'no changes'}; failed steps: {', '.join(failed) or 'none'}"
    node.log(f"restored {run_id} ({trigger}): {summary}")
    return {"restored": True, **record}


def cmd_boot_restore(node: Node) -> dict[str, Any]:
    """ExecStart of sharpwire-restore.service, which only starts when the marker exists."""
    run_id = armed_run(node)
    if run_id is None:
        return {"restored": False}
    if not RUN_ID.fullmatch(run_id):
        # Nothing to restore from. Leave the marker so the controller sees the node was armed.
        node.log(f"boot-restore: marker names {run_id!r}, which isn't a run ID; not restoring")
        raise HelperError("The marker doesn't name a run, so nothing was restored.", "no_snapshot")
    return cmd_restore(node, run_id, "boot")


# ---------------------------------------------------------------------------------------------
# Reads


def cmd_status(node: Node, run_id: str | None = None) -> dict[str, Any]:
    """`armed` with neither `timer_active` nor `restore_active` means the timer is gone: the node
    restores at its next boot, and the controller should restore or disarm it now.
    """
    armed = armed_run(node)
    status: dict[str, Any] = {
        "version": VERSION,
        "armed": armed,
        "timer_active": armed is not None and unit_active(node, f"{timer_unit(armed)}.timer"),
        "restore_active": armed is not None and unit_active(node, f"{timer_unit(armed)}.service"),
        "restore_service_enabled": node.run((SYSTEMCTL, "is-enabled", "--quiet", RESTORE_SERVICE)).rc == 0,
        "interfaces_new": node.path(INTERFACES_NEW).exists(),
    }
    if run_id is not None:
        check_run_id(run_id)
        failed = node.run_dir(run_id) / "restore-failed.json"
        status["run"] = {
            "id": run_id,
            "snapshot": (node.run_dir(run_id) / "manifest.json").exists(),
            "restored": restore_record(node, run_id),
            "restore_failed": json.loads(failed.read_text(encoding="utf-8")) if failed.exists() else None,
        }
    return status


def sysctl_keys(text: str) -> list[str]:
    """The keys a sysctl.d file sets, as `sysctl` takes them."""
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip().lstrip("-"))
    return keys


def read_file(node: Node, name: str) -> dict[str, Any]:
    try:
        found = read_regular(node.path(name))
    except NotARegularFile:
        return {"present": True, "regular": False}
    if found is None:
        return {"present": False}
    data, st = found
    return {
        "present": True,
        "regular": True,
        "mode": stat.S_IMODE(st.st_mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
        # Bytes that aren't UTF-8 come through as lone surrogates, so they still show up as drift.
        "content": data.decode("utf-8", errors="surrogateescape"),
    }


def cmd_read(node: Node) -> dict[str, Any]:
    """The managed files and the live state, raw. Read-only: it never reloads or writes anything."""
    files = {name: read_file(node, name) for name in MANAGED_FILES}
    commands = dict(LIVE_COMMANDS)
    sysctl_file = files["/etc/sysctl.d/90-sdn.conf"]
    if "content" in sysctl_file and (keys := sysctl_keys(sysctl_file["content"])):
        # The running values of the keys the file sets; -e skips keys this kernel doesn't have.
        commands["sysctl"] = (SYSCTL, "-e", "--", *keys)
    live = {}
    for name, argv in commands.items():
        result = node.run(argv)
        live[name] = {"argv": list(argv), "rc": result.rc, "stdout": result.stdout, "stderr": result.stderr}
    return {"status": cmd_status(node), "files": files, "live": live}


# ---------------------------------------------------------------------------------------------
# CLI


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise HelperError(f"{self.prog}: {message}", "usage")


def parser() -> argparse.ArgumentParser:
    p = Parser(prog="sharpwire-node", description=(__doc__ or "").split("\n\n")[0], allow_abbrev=False)
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    sub.add_parser("version", help="print the helper's version")
    status = sub.add_parser("status", help="the rollback state, and one run's when --run is given")
    status.add_argument("--run")
    sub.add_parser("read", help="the managed files and the live state the drift check needs")
    snapshot = sub.add_parser("snapshot", help="snapshot the managed files and the generated frr.conf")
    snapshot.add_argument("run")
    arm = sub.add_parser("arm", help="arm the rollback: it restores the snapshot unless disarmed in time")
    arm.add_argument("run")
    arm.add_argument("seconds", type=int)
    disarm = sub.add_parser("disarm", help="stop the rollback timer and remove the marker")
    disarm.add_argument("run")
    restore = sub.add_parser("restore", help="restore an armed run's snapshot now, and reload")
    restore.add_argument("run")
    restore.add_argument("--trigger", choices=("controller", "timer"), default="controller")
    sub.add_parser("boot-restore", help="sharpwire-restore.service: restore the armed snapshot at boot")
    return p


def dispatch(node: Node, args: argparse.Namespace) -> dict[str, Any]:
    match args.command:
        case "version":
            return {"version": VERSION}
        case "status":
            return cmd_status(node, args.run)
        case "read":
            return cmd_read(node)
        case "snapshot":
            return cmd_snapshot(node, args.run)
        case "arm":
            return cmd_arm(node, args.run, args.seconds)
        case "disarm":
            return cmd_disarm(node, args.run)
        case "restore":
            return cmd_restore(node, args.run, args.trigger)
        case "boot-restore":
            return cmd_boot_restore(node)
    raise AssertionError(args.command)


def emit(payload: dict[str, Any]) -> None:
    # The controller may have gone away; what happened is in the journal and the run's records.
    with contextlib.suppress(BrokenPipeError):
        print(json.dumps(payload, sort_keys=True), flush=True)


def main(argv: Sequence[str] | None = None, node: Node | None = None) -> int:
    node = node or Node()
    # A dropped SSH session must not kill a command halfway through changing the rollback state.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    command = "?"
    try:
        args = parser().parse_args(argv)
        command = args.command
        result = dispatch(node, args)
    except HelperError as exc:
        node.log(f"{command} failed: {exc.detail}")
        emit({"detail": exc.detail, "error_code": exc.error_code})
        return 1
    except Exception:
        node.log(f"{command} failed: {traceback.format_exc()}")
        emit(
            {
                "detail": f"sharpwire-node {command} failed unexpectedly. See the node's journal.",
                "error_code": "internal",
            }
        )
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
