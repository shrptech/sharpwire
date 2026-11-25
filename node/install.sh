#!/bin/bash
# sharpwire-node bootstrap: installs or upgrades the helper on one Proxmox node. Run it as root from the
# unpacked tarball. Re-running it upgrades the helper in place and is otherwise a no-op.
#
#   First install:  ./install.sh --key "ssh-ed25519 AAAA… sharpwire" --from 2001:db8:100::5
#   Upgrade:        ./install.sh
#   Rotate the key: ./install.sh --key "<new key>" --from <controller address>
#
# --key is the controller's SSH public key and --from its address on the mgmt network. The key is only
# accepted from that address, with no pty or forwarding (docs/07-security-and-observability.md).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
user=sharpwire
helper=/usr/local/sbin/sharpwire-node
sudoers=/etc/sudoers.d/sharpwire
unit=/etc/systemd/system/sharpwire-restore.service
state=/var/lib/sharpwire

key=
from=
while [ $# -gt 0 ]; do
  case $1 in
    --key) key=${2:?--key needs a value}; shift 2 ;;
    --from) from=${2:?--from needs a value}; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

die() { echo "install.sh: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run this as root."
[ -n "$key" ] && [ -z "$from" ] && die "--key needs --from, the controller's mgmt address."
[ -z "$key" ] && [ -n "$from" ] && die "--from needs --key."
if [ -n "$from" ]; then
  # An address or a CIDR, IPv4 or IPv6. It lands in authorized_keys inside quotes, so no quotes or commas.
  [[ $from =~ ^[0-9A-Fa-f:.]+(/[0-9]{1,3})?$ ]] || die "--from must be an IP address or prefix, not '$from'."
fi

# Check the key before touching anything, so a bad one leaves the node as it was.
if [ -n "$key" ]; then
  [[ $key != *$'\n'* ]] || die "--key must be one public key on one line."
  # A bare key: options such as command= or from= would come from the operator, not from us.
  [[ $key =~ ^(ssh-|ecdsa-|sk-) ]] || die "--key must start with the key type (ssh-ed25519 …), without options."
  keyfile=$(mktemp)
  trap 'rm -f "$keyfile"' EXIT
  printf '%s\n' "$key" > "$keyfile"
  ssh-keygen -l -f "$keyfile" >/dev/null 2>&1 || die "--key isn't an SSH public key."
fi

command -v sudo >/dev/null || die "sudo isn't installed. Run: apt install sudo"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 13))' \
  || die "the helper needs python3 3.13 or newer (Proxmox VE 9)."

# Replacing the helper mid-apply could strand an armed rollback.
if [ -e "$state/armed" ]; then
  die "run $(cat "$state/armed") is armed. Wait for the apply to finish, or disarm it, then re-run."
fi

new_version=$(python3 -I "$here/sharpwire_node.py" version | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])')
if [ -e "$helper" ]; then
  # A helper that doesn't run (damaged, say) is replaced, not a reason to stop.
  old_version=$("$helper" version 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])' 2>/dev/null) \
    || { echo "install.sh: warning: the installed helper doesn't run; replacing it." >&2; old_version=0; }
  [ "$new_version" -ge "$old_version" ] || die "this package has helper $new_version, older than the installed $old_version."
fi

# The user: a system account with a locked password and a shell, since the controller runs commands over SSH.
if ! id "$user" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$user" --shell /bin/sh --comment "sharpwire controller" "$user"
fi

install -d -m 0700 -o root -g root "$state"
install -m 0755 -o root -g root "$here/sharpwire_node.py" "$helper.new"
sync "$helper.new"
mv -f "$helper.new" "$helper"
sync "$(dirname "$helper")"

# visudo checks the rule before it goes live; a broken file in sudoers.d breaks sudo for everyone.
install -m 0440 -o root -g root "$here/sudoers" "$sudoers.new"
visudo -cqf "$sudoers.new" || { rm -f "$sudoers.new"; die "the sudoers rule doesn't parse; nothing changed there."; }
mv -f "$sudoers.new" "$sudoers"

install -m 0644 -o root -g root "$here/sharpwire-restore.service" "$unit"
systemctl daemon-reload
# reenable, not enable: it also fixes the symlinks if [Install] changed between versions.
systemctl reenable --quiet sharpwire-restore.service

if [ -n "$key" ]; then
  home=$(getent passwd "$user" | cut -d: -f6)
  install -d -m 0700 -o "$user" -g "$user" "$home/.ssh"
  # Built in root's temp file, then installed: root never writes through a path in the user's directory.
  printf 'restrict,from="%s" %s\n' "$from" "$key" > "$keyfile"
  rm -f "$home/.ssh/authorized_keys.new"
  install -m 0600 -o "$user" -g "$user" "$keyfile" "$home/.ssh/authorized_keys.new"
  mv -f "$home/.ssh/authorized_keys.new" "$home/.ssh/authorized_keys"
fi

home=$(getent passwd "$user" | cut -d: -f6)
[ -s "$home/.ssh/authorized_keys" ] || echo "install.sh: warning: no controller key yet. Re-run with --key and --from." >&2

# Prove the path the controller uses works end to end.
sudo -n -u "$user" sudo -n "$helper" version >/dev/null || die "the sharpwire user can't run the helper through sudo."
echo "sharpwire-node $new_version installed."
