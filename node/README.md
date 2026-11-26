# sharpwire-node

The bootstrap package for Proxmox nodes. It installs:

| What | Where |
| --- | --- |
| The `sharpwire` user, with the controller's SSH key | `/home/sharpwire/.ssh/authorized_keys`, as `restrict,from="<controller>"` |
| The root helper, `sharpwire-node` | `/usr/local/sbin/sharpwire-node` |
| The sudo rule letting `sharpwire` run only the helper | `/etc/sudoers.d/sharpwire` |
| The boot-time restore | `/etc/systemd/system/sharpwire-restore.service`, enabled |
| Rollback snapshots and the armed marker | `/var/lib/sharpwire/` (root, 0700) |

The design is in [Drivers](../docs/04-drivers.md#node-targets-host-files) and [Apply and rollout](../docs/05-apply-and-rollout.md#proxmox-nodes).

## Install or upgrade

Build the tarball, copy it to the node, then run `install.sh` as root:

```sh
uv run python node/build.py               # dist/sharpwire-node-<version>.tar.gz
scp dist/sharpwire-node-1.tar.gz root@pve1:
ssh root@pve1
tar xzf sharpwire-node-1.tar.gz && cd sharpwire-node-1
./install.sh --key "ssh-ed25519 AAAA… sharpwire" --from 2001:db8:100::5   # first install
./install.sh                                                               # upgrade
```

`--key` is the controller's public key and `--from` its mgmt address. Pass both again to rotate the key. The node needs `sudo` (`apt install sudo`) and Python 3.13 or newer, which PVE 9 ships. `install.sh` refuses to run while a rollback is armed, and refuses to downgrade the helper.

## The helper

The controller runs `sudo /usr/local/sbin/sharpwire-node <command>` over SSH. Every command prints one JSON object. A failure exits 1 and prints `{"detail": ..., "error_code": ...}`.

| Command | Does |
| --- | --- |
| `version` | The helper's version. The controller refuses to plan for a node whose helper is older than it supports |
| `status [--run RUN]` | Which run is armed, whether its timer or its restore is running (a restore waiting to retry counts), whether `sharpwire-restore.service` is enabled, whether `interfaces.new` is pending, the armed run's failed restore if any, and one run's snapshot and restore records. Armed with neither timer nor restore running means the timer is gone: restore or disarm the node now, or it restores at its next boot |
| `read` | The managed files (a symlink is reported, never followed; a file over 4 MiB isn't read) and the live state, raw: `ifquery` and `ifquery --check`, `ip` link, address and default routes, the sysctl keys the managed file sets, and FRR's running config, BGP summary and BFD peers. It writes nothing |
| `snapshot RUN` | Copies the managed files and the generated `/etc/frr/frr.conf`, with owner and mode, to `/var/lib/sharpwire/rollback/RUN/` |
| `arm RUN SECONDS` | Checks the snapshot is complete, writes the marker (run and boot ID) durably, then starts the transient timer `sharpwire-rollback-RUN`, whose restore retries every 30 s |
| `disarm RUN` | Removes the marker, then stops the timer. Fails with `rollback_fired` if the restore already ran, and `rollback_incomplete` (staying armed) if it fired and is still retrying |
| `restore RUN` | Restores the armed snapshot now, then reloads networking, sysctl and FRR (`frr-reload.py`, falling back to a restart; an enabled FRR that's stopped is started). The timer is stopped once it succeeds |
| `boot-restore` | Run by `sharpwire-restore.service`: restores without reloading, and re-runs `systemd-sysctl`. It does nothing for a run armed in the current boot (`systemctl isolate` starts the unit again) |

A restore puts back every file it can. Until it's done, the run stays armed, the latest failure goes to `rollback/RUN/restore-failed.json` and the restore exits 1, so the timer unit retries it and the next boot tries again. A file that can't be written stops the attempt before any reload. Once every file is back, failed reloads are retried for 5 attempts in all; then the restore records `rollback/RUN/restored.json` with `complete: false` and clears the marker. A successful one records it with `complete: true`. `status` and `disarm` report both records. The timer's and the boot's restores wait for the lock however long it takes; `disarm` does too, so it never reports `busy` for a rollback that's firing. The 20 newest snapshots are kept. The helper holds no rules: the controller decides what the files should contain and whether the node has drifted.

Writing and applying the files comes in build plan step 6, as a helper upgrade.

Everything here is tested without a node (`tests/node/`), on the controller's Python and on 3.13. Before the first production install, check on the nested PVE lab:
- the `ifquery --format json` output;
- the unit's ordering against the real PVE units: `systemd-analyze verify sharpwire-restore.service`, and no `ordering cycle` in `journalctl -b`;
- that `systemd-run` takes `StartLimitIntervalSec=0` and `Restart=on-failure` for the transient restore;
- `install.sh` on a fresh node, an upgrade, and a re-key.
