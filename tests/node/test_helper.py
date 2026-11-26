"""The sharpwire-node helper, against a temporary root and a fake command runner."""

import fcntl
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from node import sharpwire_node as helper
from node.sharpwire_node import HelperError, Node, Result


class FakeRunner:
    """Records every command, with the state of the marker when it ran, and answers rc 0 by default."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.results: dict[tuple[str, ...], Result] = {}
        self.hooks: dict[str, Callable[[tuple[str, ...]], Result | None]] = {}
        self.marker_at_call: list[bool] = []
        self.marker: Path | None = None
        # `systemctl show --property=ActiveState` answers, per unit. Anything not listed is inactive.
        self.states: dict[str, str] = {}

    def __call__(self, argv: Sequence[str], *, timeout: int = helper.COMMAND_TIMEOUT_SECONDS) -> Result:
        call = tuple(argv)
        self.calls.append(call)
        self.marker_at_call.append(self.marker is not None and self.marker.exists())
        if (hook := self.hooks.get(call[0])) is not None and (result := hook(call)) is not None:
            return result
        if call in self.results:
            return self.results[call]
        if call[:4] == (helper.SYSTEMCTL, "show", "--property=ActiveState", "--value"):
            return Result(0, f"{self.states.get(call[4], 'inactive')}\n", "")
        return Result(0, "", "")

    def marker_when(self, call: tuple[str, ...]) -> bool:
        return self.marker_at_call[self.calls.index(call)]

    def fail(self, *argv: str, rc: int = 1, stderr: str = "") -> None:
        self.results[argv] = Result(rc, "", stderr)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def node(tmp_path: Path, runner: FakeRunner) -> Node:
    files = {
        "/etc/network/interfaces": "auto lo\niface lo inet loopback\n",
        "/etc/sysctl.d/90-sdn.conf": "# sdn\nnet.ipv6.conf.all.forwarding = 1\n-net.ipv4.ip_forward=1\n",
        "/etc/frr/frr.conf.local": "route-map UL-IN permit 10\n",
        "/etc/hosts": "127.0.0.1 localhost\n",
        "/etc/frr/frr.conf": "frr version 10.6.1\n",
    }
    for name, content in files.items():
        path = tmp_path / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (tmp_path / "etc/frr/frr.conf").chmod(0o640)
    boot_id = tmp_path / "proc/sys/kernel/random/boot_id"
    boot_id.parent.mkdir(parents=True)
    boot_id.write_text("boot-1\n")
    node = Node(
        root=tmp_path,
        run=runner,
        log=lambda _message: None,
        self_path="/usr/local/sbin/sharpwire-node",
        now=lambda: datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
    )
    runner.marker = node.marker
    return node


def reboot(node: Node) -> None:
    node.path(helper.BOOT_ID).write_text("boot-2\n")


def armed(node: Node, run_id: str = "run-1", seconds: int = 600) -> None:
    helper.cmd_snapshot(node, run_id)
    helper.cmd_arm(node, run_id, seconds)


def break_files(node: Node) -> None:
    node.path("/etc/network/interfaces").write_text("broken\n")
    node.path("/etc/frr/frr.conf").write_text("broken\n")
    node.path("/etc/frr/frr.conf.local").unlink()


# --- snapshot ----------------------------------------------------------------------------------


def test_snapshot_copies_the_managed_files_and_the_generated_frr_conf(node: Node) -> None:
    node.path("/etc/hosts").unlink()

    result = helper.cmd_snapshot(node, "run-1")

    manifest = json.loads((node.run_dir("run-1") / "manifest.json").read_text())
    assert [f["path"] for f in manifest["files"]] == [*helper.MANAGED_FILES, helper.GENERATED_FRR_CONF]
    hosts = next(f for f in manifest["files"] if f["path"] == "/etc/hosts")
    assert hosts == {"path": "/etc/hosts", "present": False}
    frr = next(f for f in manifest["files"] if f["path"] == "/etc/frr/frr.conf")
    assert frr["mode"] == 0o640
    assert (node.run_dir("run-1") / "files/etc/frr/frr.conf").read_text() == "frr version 10.6.1\n"
    assert result["run"] == "run-1"
    assert stat.S_IMODE(node.state.stat().st_mode) == 0o700


def test_snapshot_refuses_an_existing_run(node: Node) -> None:
    helper.cmd_snapshot(node, "run-1")
    with pytest.raises(HelperError) as exc:
        helper.cmd_snapshot(node, "run-1")
    assert exc.value.error_code == "snapshot_exists"


def test_snapshot_refuses_while_another_run_is_armed(node: Node) -> None:
    armed(node, "run-1")
    with pytest.raises(HelperError) as exc:
        helper.cmd_snapshot(node, "run-2")
    assert exc.value.error_code == "already_armed"


@pytest.mark.parametrize("run_id", ["", "../etc", "a b", "x" * 65, "run;rm", "-abc", "_abc"])
def test_run_ids_are_checked(node: Node, run_id: str) -> None:
    with pytest.raises(HelperError) as exc:
        helper.cmd_snapshot(node, run_id)
    assert exc.value.error_code == "invalid_run_id"


# --- arm and disarm ------------------------------------------------------------------------------


def test_arm_writes_the_marker_then_starts_the_timer(node: Node, runner: FakeRunner) -> None:
    helper.cmd_snapshot(node, "run-1")

    helper.cmd_arm(node, "run-1", 600)

    assert node.marker.read_text() == "run-1\nboot-1\n"
    timer = runner.calls[-1]
    assert timer == (
        helper.SYSTEMD_RUN,
        "--quiet",
        "--collect",
        "--on-active=600s",
        "--timer-property=AccuracySec=1s",
        "--unit=sharpwire-rollback-run-1",
        "--property=Restart=on-failure",
        "--property=RestartSec=30s",
        "--property=StartLimitIntervalSec=0",
        "--",
        "/usr/local/sbin/sharpwire-node",
        "restore",
        "--trigger",
        "timer",
        "--",
        "run-1",
    )
    assert runner.marker_when(timer), "the marker must be durable before the timer exists"


def test_the_timer_command_parses_as_the_restore_it_means(node: Node) -> None:
    args = helper.parser().parse_args(["restore", "--trigger", "timer", "--", "run-1"])
    assert (args.command, args.run, args.trigger) == ("restore", "run-1", "timer")


def test_arm_needs_a_snapshot(node: Node) -> None:
    with pytest.raises(HelperError) as exc:
        helper.cmd_arm(node, "run-1", 600)
    assert exc.value.error_code == "no_snapshot"
    assert not node.marker.exists()


def test_arm_needs_the_restore_service(node: Node, runner: FakeRunner) -> None:
    runner.fail(helper.SYSTEMCTL, "is-enabled", "--quiet", helper.RESTORE_SERVICE)
    helper.cmd_snapshot(node, "run-1")
    with pytest.raises(HelperError) as exc:
        helper.cmd_arm(node, "run-1", 600)
    assert exc.value.error_code == "not_installed"
    assert not node.marker.exists()


@pytest.mark.parametrize("seconds", [0, 59, 3601])
def test_arm_checks_the_delay(node: Node, seconds: int) -> None:
    helper.cmd_snapshot(node, "run-1")
    with pytest.raises(HelperError) as exc:
        helper.cmd_arm(node, "run-1", seconds)
    assert exc.value.error_code == "invalid_delay"


def test_a_failed_timer_leaves_nothing_armed(node: Node, runner: FakeRunner) -> None:
    helper.cmd_snapshot(node, "run-1")
    runner.hooks[helper.SYSTEMD_RUN] = lambda _call: Result(1, "", "Failed to start transient timer unit")
    with pytest.raises(HelperError) as exc:
        helper.cmd_arm(node, "run-1", 600)
    assert exc.value.error_code == "arm_failed"
    assert not node.marker.exists()


def test_arm_refuses_a_second_run(node: Node) -> None:
    armed(node, "run-1")
    # run-2 can't snapshot while run-1 is armed, so fake its snapshot to reach arm's own check.
    node.run_dir("run-2").mkdir(parents=True)
    (node.run_dir("run-2") / "manifest.json").write_text("{}")
    with pytest.raises(HelperError) as exc:
        helper.cmd_arm(node, "run-2", 600)
    assert exc.value.error_code == "already_armed"
    assert node.marker.read_text() == "run-1\nboot-1\n"


def test_disarm_removes_the_marker_then_stops_the_timer(node: Node, runner: FakeRunner) -> None:
    armed(node)

    helper.cmd_disarm(node, "run-1")

    assert not node.marker.exists()
    stop = (helper.SYSTEMCTL, "stop", "sharpwire-rollback-run-1.timer")
    # The marker goes first: a helper killed in between leaves a timer that finds nothing armed, not a marker that
    # would roll a good apply back at the next boot.
    assert not runner.marker_when(stop)


def test_arm_refuses_a_damaged_snapshot(node: Node) -> None:
    helper.cmd_snapshot(node, "run-1")
    (node.run_dir("run-1") / "files/etc/hosts").write_text("trunc")
    with pytest.raises(HelperError) as exc:
        helper.cmd_arm(node, "run-1", 600)
    assert exc.value.error_code == "snapshot_damaged"
    assert not node.marker.exists()


def test_disarm_after_the_rollback_fired_fails(node: Node) -> None:
    armed(node)
    helper.cmd_restore(node, "run-1", "timer")

    with pytest.raises(HelperError) as exc:
        helper.cmd_disarm(node, "run-1")
    assert exc.value.error_code == "rollback_fired"


def test_disarm_of_a_run_that_was_never_armed_fails(node: Node) -> None:
    with pytest.raises(HelperError) as exc:
        helper.cmd_disarm(node, "run-1")
    assert exc.value.error_code == "not_armed"


def test_a_restored_run_cant_be_armed_again(node: Node) -> None:
    armed(node)
    helper.cmd_restore(node, "run-1", "controller")
    with pytest.raises(HelperError) as exc:
        helper.cmd_arm(node, "run-1", 600)
    assert exc.value.error_code == "rollback_fired"


# --- restore -------------------------------------------------------------------------------------


def test_restore_puts_the_snapshot_back_with_owner_and_mode(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    armed(node)
    manifest = json.loads((node.run_dir("run-1") / "manifest.json").read_text())
    for entry in manifest["files"]:
        if entry["path"] == "/etc/frr/frr.conf":
            entry["uid"], entry["gid"] = 100, 101  # frr:frr on a node
    (node.run_dir("run-1") / "manifest.json").write_text(json.dumps(manifest))
    owners: dict[str, tuple[int, int]] = {}
    real_open = os.open

    def recording_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        names[fd] = Path(path).name
        return fd

    names: dict[int, str] = {}
    monkeypatch.setattr(helper.os, "open", recording_open)
    monkeypatch.setattr(helper.os, "fchown", lambda fd, uid, gid: owners.__setitem__(names[fd], (uid, gid)))
    break_files(node)
    node.path("/etc/frr/frr.conf").chmod(0o600)
    node.path("/etc/network/interfaces.new").write_text("pending\n")

    result = helper.cmd_restore(node, "run-1", "controller")

    assert node.path("/etc/network/interfaces").read_text() == "auto lo\niface lo inet loopback\n"
    assert node.path("/etc/frr/frr.conf.local").read_text() == "route-map UL-IN permit 10\n"
    assert node.path("/etc/frr/frr.conf").read_text() == "frr version 10.6.1\n"
    assert stat.S_IMODE(node.path("/etc/frr/frr.conf").stat().st_mode) == 0o640
    assert owners[".frr.conf.sharpwire-tmp"] == (100, 101)
    assert not node.path("/etc/network/interfaces.new").exists()
    assert node.path("/run/network").is_dir()
    assert not node.marker.exists()
    assert result["restored"] is True
    assert "/etc/network/interfaces.new" in result["files"]
    assert not list(node.path("/etc/frr").glob(".*sharpwire-tmp"))


def test_restore_removes_a_file_that_was_absent_at_snapshot(node: Node) -> None:
    node.path("/etc/hosts").unlink()
    armed(node)
    node.path("/etc/hosts").write_text("new\n")

    helper.cmd_restore(node, "run-1", "controller")

    assert not node.path("/etc/hosts").exists()


def test_restore_reloads_the_way_pve_does(node: Node, runner: FakeRunner) -> None:
    armed(node)
    runner.calls.clear()
    runner.states["frr.service"] = "active"
    runner.fail(helper.FRR_RELOAD, "--reload", "/etc/frr/frr.conf")

    result = helper.cmd_restore(node, "run-1", "timer")

    assert runner.calls == [
        (helper.IFRELOAD, "-a"),
        (helper.SYSCTL, "--quiet", "--system"),
        (helper.SYSTEMCTL, "show", "--property=ActiveState", "--value", "frr.service"),
        (helper.FRR_RELOAD, "--reload", "/etc/frr/frr.conf"),
        (helper.SYSTEMCTL, "restart", "frr.service"),
        (helper.SYSTEMCTL, "stop", "sharpwire-rollback-run-1.timer"),
    ]
    # The restart, PVE's fallback, worked: the reload counts as done.
    assert result["steps"] == {"ifreload": 0, "sysctl": 0, "frr_reload": 1, "frr_restart": 0}
    assert result["complete"] is True
    assert not node.marker.exists()
    record = json.loads((node.run_dir("run-1") / "restored.json").read_text())
    assert record["trigger"] == "timer"
    assert record["at"] == "2026-11-25T12:00:00+00:00"


def test_restore_starts_an_enabled_frr_the_bad_config_stopped(node: Node, runner: FakeRunner) -> None:
    armed(node)
    runner.states["frr.service"] = "failed"

    result = helper.cmd_restore(node, "run-1", "controller")

    assert (helper.SYSTEMCTL, "reset-failed", "frr.service") in runner.calls
    assert result["steps"]["frr_start"] == 0 and "frr_reload" not in result["steps"]


def test_restore_leaves_a_disabled_frr_alone(node: Node, runner: FakeRunner) -> None:
    armed(node)
    runner.fail(helper.SYSTEMCTL, "is-enabled", "--quiet", "frr.service")

    result = helper.cmd_restore(node, "run-1", "controller")

    assert not {"frr_reload", "frr_start"} & set(result["steps"])


def test_a_controller_restore_stops_the_timer(node: Node, runner: FakeRunner) -> None:
    armed(node)
    helper.cmd_restore(node, "run-1", "controller")
    assert (helper.SYSTEMCTL, "stop", "sharpwire-rollback-run-1.timer") in runner.calls


def test_a_restore_that_cant_write_a_file_restores_the_rest_and_stays_armed(node: Node) -> None:
    armed(node)
    break_files(node)
    node.path("/etc/hosts").unlink()
    node.path("/etc/hosts").mkdir()  # os.replace can't put a file over it

    with pytest.raises(HelperError) as exc:
        helper.cmd_restore(node, "run-1", "timer")

    assert exc.value.error_code == "restore_incomplete"
    assert node.path("/etc/network/interfaces").read_text() == "auto lo\niface lo inet loopback\n"
    assert node.marker.exists(), "still armed, so the timer unit retries and the next boot restores"
    failed = helper.cmd_status(node, "run-1")["run"]["restore_failed"]
    assert list(failed["errors"]) == ["/etc/hosts"]

    # The retry, once the obstacle is gone (here after a reboot), finishes the job.
    node.path("/etc/hosts").rmdir()
    reboot(node)
    assert helper.cmd_boot_restore(node)["complete"] is True
    assert node.path("/etc/hosts").read_text() == "127.0.0.1 localhost\n"
    assert not node.marker.exists()


def test_a_damaged_snapshot_is_refused_before_anything_is_written(node: Node) -> None:
    armed(node)
    break_files(node)
    (node.run_dir("run-1") / "files/etc/frr/frr.conf").unlink()

    with pytest.raises(HelperError) as exc:
        helper.cmd_restore(node, "run-1", "timer")

    assert exc.value.error_code == "snapshot_damaged"
    assert node.path("/etc/network/interfaces").read_text() == "broken\n"
    assert node.marker.exists()


def test_restore_never_writes_through_a_planted_symlink(node: Node, tmp_path: Path) -> None:
    armed(node)
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    (node.path("/etc/frr") / ".frr.conf.sharpwire-tmp").symlink_to(victim)

    helper.cmd_restore(node, "run-1", "controller")

    assert victim.read_text() == "precious\n"
    assert not node.path("/etc/frr/frr.conf").is_symlink()
    assert node.path("/etc/frr/frr.conf").read_text() == "frr version 10.6.1\n"


def test_snapshot_refuses_a_symlinked_managed_file(node: Node, tmp_path: Path) -> None:
    node.path("/etc/frr/frr.conf.local").unlink()
    node.path("/etc/frr/frr.conf.local").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(HelperError) as exc:
        helper.cmd_snapshot(node, "run-1")
    assert exc.value.error_code == "not_a_file"
    assert not node.run_dir("run-1").exists()


def test_a_timer_restore_after_disarm_does_nothing(node: Node) -> None:
    armed(node)
    helper.cmd_disarm(node, "run-1")
    break_files(node)

    result = helper.cmd_restore(node, "run-1", "timer")

    assert result == {"run": "run-1", "restored": False}
    assert node.path("/etc/network/interfaces").read_text() == "broken\n"


def test_a_controller_restore_of_an_unarmed_run_fails(node: Node) -> None:
    helper.cmd_snapshot(node, "run-1")
    with pytest.raises(HelperError) as exc:
        helper.cmd_restore(node, "run-1", "controller")
    assert exc.value.error_code == "not_armed"


def test_boot_restore_restores_without_reloading(node: Node, runner: FakeRunner) -> None:
    armed(node)
    break_files(node)
    reboot(node)
    runner.calls.clear()

    result = helper.cmd_boot_restore(node)

    assert runner.calls == [(helper.SYSTEMD_SYSCTL,), (helper.SYSTEMCTL, "stop", "sharpwire-rollback-run-1.timer")]
    assert result["trigger"] == "boot"
    assert node.path("/etc/network/interfaces").read_text() == "auto lo\niface lo inet loopback\n"
    assert not node.marker.exists()


def test_boot_restore_with_nothing_armed_does_nothing(node: Node, runner: FakeRunner) -> None:
    assert helper.cmd_boot_restore(node) == {"restored": False}
    assert runner.calls == []


def test_boot_restore_without_a_snapshot_keeps_the_marker(node: Node) -> None:
    node.state.mkdir(parents=True)
    node.marker.write_text("run-9\nboot-0\n")
    with pytest.raises(HelperError) as exc:
        helper.cmd_boot_restore(node)
    assert exc.value.error_code == "no_snapshot"
    assert node.marker.exists()


# --- reads ---------------------------------------------------------------------------------------


def test_status_reports_the_rollback_state(node: Node, runner: FakeRunner) -> None:
    armed(node)
    node.path("/etc/network/interfaces.new").write_text("pending\n")
    runner.states["sharpwire-rollback-run-1.timer"] = "active"

    status = helper.cmd_status(node, "run-1")

    assert status == {
        "version": helper.VERSION,
        "armed": "run-1",
        "timer_active": True,
        "restore_active": False,
        "restore_service_enabled": True,
        "interfaces_new": True,
        "restore_failed": None,
        "run": {"id": "run-1", "snapshot": True, "restored": None, "restore_failed": None},
    }


def test_status_shows_an_armed_node_whose_timer_is_gone(node: Node, runner: FakeRunner) -> None:
    armed(node)
    runner.results[(helper.SYSTEMCTL, "is-enabled", "--quiet", helper.RESTORE_SERVICE)] = Result(1, "", "")

    status = helper.cmd_status(node)

    assert (status["armed"], status["timer_active"], status["restore_active"]) == ("run-1", False, False)
    assert status["restore_service_enabled"] is False


def test_read_returns_the_managed_files_and_the_live_state(node: Node, runner: FakeRunner) -> None:
    runner.results[helper.LIVE_COMMANDS["ifquery_check"]] = Result(1, "[]", "drift")

    result = helper.cmd_read(node)

    assert set(result["files"]) == set(helper.MANAGED_FILES)
    assert helper.GENERATED_FRR_CONF not in result["files"]
    assert result["files"]["/etc/hosts"]["content"] == "127.0.0.1 localhost\n"
    assert result["live"]["ifquery_check"]["rc"] == 1
    assert result["live"]["sysctl"]["argv"] == [
        helper.SYSCTL,
        "-e",
        "--",
        "net.ipv6.conf.all.forwarding",
        "net.ipv4.ip_forward",
    ]
    assert set(result["live"]) == {*helper.LIVE_COMMANDS, "sysctl"}


def test_read_reports_symlinks_and_odd_bytes_without_following_or_failing(node: Node, tmp_path: Path) -> None:
    (tmp_path / "shadow").write_text("secret\n")
    node.path("/etc/frr/frr.conf.local").unlink()
    node.path("/etc/frr/frr.conf.local").symlink_to(tmp_path / "shadow")
    node.path("/etc/hosts").write_bytes(b"10.0.0.1 h\xff\n")

    files = helper.cmd_read(node)["files"]

    assert files["/etc/frr/frr.conf.local"] == {"present": True, "regular": False}
    assert files["/etc/hosts"]["content"] == "10.0.0.1 h\udcff\n"
    json.dumps(files)


def test_read_writes_nothing(node: Node, runner: FakeRunner) -> None:
    helper.cmd_read(node)
    assert not node.state.exists()
    write_tools = {helper.IFRELOAD, helper.SYSTEMD_RUN, helper.FRR_RELOAD, helper.SYSTEMD_SYSCTL}
    assert not [call for call in runner.calls if call[0] in write_tools]
    assert all(call[1] != "--system" for call in runner.calls if call[0] == helper.SYSCTL)


# --- CLI -----------------------------------------------------------------------------------------


def test_cli_prints_json(node: Node, capsys: pytest.CaptureFixture[str]) -> None:
    assert helper.main(["version"], node) == 0
    assert json.loads(capsys.readouterr().out) == {"version": helper.VERSION}


def test_cli_errors_use_the_api_error_shape(node: Node, capsys: pytest.CaptureFixture[str]) -> None:
    assert helper.main(["disarm", "run-1"], node) == 1
    assert json.loads(capsys.readouterr().out) == {"detail": "Run run-1 isn't armed.", "error_code": "not_armed"}


def test_cli_usage_errors_are_json_too(node: Node, capsys: pytest.CaptureFixture[str]) -> None:
    assert helper.main(["arm", "run-1"], node) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "usage"


def test_cli_turns_unexpected_errors_into_json(
    node: Node, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_node: Node) -> dict[str, object]:
        raise PermissionError("nope")

    monkeypatch.setattr(helper, "cmd_read", boom)
    assert helper.main(["read"], node) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "internal"


def test_cli_end_to_end(node: Node, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["snapshot", "r1"], ["arm", "r1", "600"], ["disarm", "r1"]):
        assert helper.main(argv, node) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out[-1] == {"run": "r1", "armed": False}


# --- the lock and pruning ------------------------------------------------------------------------


def hold_lock(node: Node) -> int:
    lock = node.path("/run/sharpwire-node.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_controller_commands_give_up_on_a_held_lock(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper, "LOCK_WAIT_SECONDS", 0.2)
    fd = hold_lock(node)
    try:
        with pytest.raises(HelperError) as exc:
            helper.cmd_snapshot(node, "run-1")
        assert exc.value.error_code == "busy"
    finally:
        os.close(fd)


def test_the_timer_restore_waits_for_the_lock_however_long(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    armed(node)
    monkeypatch.setattr(helper, "LOCK_WAIT_SECONDS", 0.2)
    fd = hold_lock(node)
    threading.Timer(1.0, os.close, (fd,)).start()

    started = time.monotonic()
    result = helper.cmd_restore(node, "run-1", "timer")

    assert time.monotonic() - started >= 0.9
    assert result["restored"] is True


def test_a_disarm_during_the_timer_restore_waits_and_reports_the_rollback(node: Node, runner: FakeRunner) -> None:
    armed(node)
    outcome: dict[str, object] = {}

    def disarm() -> None:
        try:
            helper.cmd_disarm(node, "run-1")
        except HelperError as exc:
            outcome["error"] = exc.error_code

    waiting = threading.Thread(target=disarm)

    def disarm_mid_restore(_call: tuple[str, ...]) -> None:
        # The restore holds the lock while ifreload runs; the controller's disarm arrives now.
        waiting.start()
        time.sleep(0.3)
        assert waiting.is_alive(), "disarm must wait for the restore"

    runner.hooks[helper.IFRELOAD] = disarm_mid_restore
    helper.cmd_restore(node, "run-1", "timer")
    waiting.join(5)

    assert outcome == {"error": "rollback_fired"}
    assert not node.marker.exists()


def test_snapshot_keeps_only_the_newest_runs(node: Node) -> None:
    for i in range(helper.KEEP_SNAPSHOTS + 3):
        helper.cmd_snapshot(node, f"run-{i}")
        os.utime(node.run_dir(f"run-{i}"), (i, i))
    runs = {p.name for p in (node.state / "rollback").iterdir()}
    assert len(runs) == helper.KEEP_SNAPSHOTS + 1
    assert "run-0" not in runs


# --- review fixes: partial restores, retries, boots and commands -------------------------------------------------


def unwritable_hosts(node: Node) -> None:
    node.path("/etc/hosts").unlink()
    node.path("/etc/hosts").mkdir()  # os.replace can't put a file over it


def test_disarm_refuses_while_a_rollback_is_part_done(node: Node) -> None:
    armed(node)
    break_files(node)
    unwritable_hosts(node)
    with pytest.raises(HelperError):
        helper.cmd_restore(node, "run-1", "timer")

    with pytest.raises(HelperError) as exc:
        helper.cmd_disarm(node, "run-1")

    assert exc.value.error_code == "rollback_incomplete"
    assert "couldn't put back /etc/hosts" in exc.value.detail
    assert node.marker.exists(), "still armed: the retries go on"


def test_a_file_that_cant_be_written_runs_no_reloads_and_leaves_no_temp_file(node: Node, runner: FakeRunner) -> None:
    armed(node)
    unwritable_hosts(node)
    runner.calls.clear()
    with pytest.raises(HelperError):
        helper.cmd_restore(node, "run-1", "timer")
    assert not [c for c in runner.calls if c[0] in (helper.IFRELOAD, helper.FRR_RELOAD, helper.SYSCTL)]
    assert not list(node.path("/etc").glob(".hosts.sharpwire-tmp"))


def test_failed_reloads_are_retried_five_times_then_recorded_incomplete(node: Node, runner: FakeRunner) -> None:
    armed(node)
    runner.fail(helper.IFRELOAD, "-a", rc=89, stderr="Another instance of this program is already running")

    for attempt in range(1, helper.RELOAD_ATTEMPTS):
        with pytest.raises(HelperError) as exc:
            helper.cmd_restore(node, "run-1", "timer")
        assert exc.value.error_code == "restore_incomplete" and "ifreload failed" in exc.value.detail
        assert node.marker.exists()
        assert helper.cmd_status(node)["restore_failed"]["attempt"] == attempt

    # The last attempt gives up: recorded, not armed any more, and the timer is stopped.
    result = helper.cmd_restore(node, "run-1", "timer")
    assert result["complete"] is False and result["reason"] == "ifreload still failed after 5 attempts"
    assert not node.marker.exists()
    with pytest.raises(HelperError) as exc:
        helper.cmd_disarm(node, "run-1")
    assert exc.value.error_code == "rollback_fired"


def test_a_reload_that_works_on_retry_completes(node: Node, runner: FakeRunner) -> None:
    armed(node)
    runner.fail(helper.IFRELOAD, "-a", rc=1)
    with pytest.raises(HelperError):
        helper.cmd_restore(node, "run-1", "timer")
    del runner.results[(helper.IFRELOAD, "-a")]
    assert helper.cmd_restore(node, "run-1", "timer")["complete"] is True
    assert not node.marker.exists()


def test_a_failed_controller_restore_keeps_its_timer(node: Node, runner: FakeRunner) -> None:
    armed(node)
    unwritable_hosts(node)
    with pytest.raises(HelperError):
        helper.cmd_restore(node, "run-1", "controller")
    assert (helper.SYSTEMCTL, "stop", "sharpwire-rollback-run-1.timer") not in runner.calls
    assert node.marker.exists()


def test_status_counts_a_retrying_restore_as_active(node: Node, runner: FakeRunner) -> None:
    armed(node)
    unwritable_hosts(node)
    with pytest.raises(HelperError):
        helper.cmd_restore(node, "run-1", "timer")
    # Between retries the service waits in auto-restart ("activating") and the timer has already elapsed.
    runner.states["sharpwire-rollback-run-1.service"] = "activating"

    status = helper.cmd_status(node)

    assert (status["timer_active"], status["restore_active"]) == (False, True)
    assert status["restore_failed"]["reason"] == "couldn't put back /etc/hosts"


def test_boot_restore_in_the_boot_the_run_was_armed_in_does_nothing(node: Node) -> None:
    # `systemctl isolate multi-user.target` while armed starts the boot unit again: it isn't a boot.
    armed(node)
    break_files(node)
    assert helper.cmd_boot_restore(node) == {"run": "run-1", "restored": False}
    assert node.marker.exists()
    assert node.path("/etc/network/interfaces").read_text() == "broken\n"


def test_the_marker_is_on_disk_before_the_timer_exists(
    node: Node, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper.cmd_snapshot(node, "run-1")
    synced: list[str] = []
    real_fsync = os.fsync
    paths: dict[int, str] = {}
    real_open = os.open

    def recording_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        paths[fd] = str(path)
        return fd

    def recording_fsync(fd: int) -> None:
        synced.append(paths.get(fd, "?"))
        real_fsync(fd)

    def at_systemd_run(_call: tuple[str, ...]) -> Result:
        # The marker's data (via its temp file) and its directory entry are both synced by now.
        assert any(p.endswith(".armed.sharpwire-tmp") for p in synced)
        assert str(node.state) in synced
        return Result(0, "", "")

    monkeypatch.setattr(helper.os, "open", recording_open)
    monkeypatch.setattr(helper.os, "fsync", recording_fsync)
    runner.hooks[helper.SYSTEMD_RUN] = at_systemd_run
    helper.cmd_arm(node, "run-1", 600)


def test_huge_files_arent_read(node: Node, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper, "MAX_FILE_BYTES", 16)
    node.path("/etc/hosts").write_text("x" * 64)
    assert helper.cmd_read(node)["files"]["/etc/hosts"] == {"present": True, "regular": True, "too_large": True}
    with pytest.raises(HelperError) as exc:
        helper.cmd_snapshot(node, "run-1")
    assert exc.value.error_code == "too_large"


def test_a_background_child_holding_the_output_doesnt_hang_a_command() -> None:
    import time as clock

    started = clock.monotonic()
    result = helper.run_command(["/bin/sh", "-c", "sleep 30 & echo done"], timeout=10)
    assert result.rc == 0 and result.stdout == "done\n"
    assert clock.monotonic() - started < 5


def test_a_command_past_its_timeout_is_killed_with_its_children() -> None:
    result = helper.run_command(["/bin/sh", "-c", "sleep 30; echo never"], timeout=1)
    assert result.rc == 124 and "timed out" in result.stderr
