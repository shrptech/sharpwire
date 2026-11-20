#!/bin/sh
# Runs INSIDE the sw-chr-lab container. Usage: lab.sh net | lab.sh boot <name> <mgmt-tap> <seg-tap>
# Image/disks live in /lab inside the container: colima only shares $HOME, so the scratchpad image is
# copied in with `docker cp <scratchpad>/chr/chr-7.25.1-arm64.img sw-chr:/lab/` (bind mount of /private/tmp is empty).
# Container: docker run -d --name sw-chr --privileged -v $PWD:/spike sw-chr-lab sleep infinity
set -e
VER=${VER:-7.25.1}
case "$1" in
net)
  # br-mgmt: container 192.168.88.2 <-> CHR ether1; br-seg: shared "VLAN 300" segment (CHR ether2 + FRR netns peers)
  for br in br-mgmt br-seg; do ip link add $br type bridge 2>/dev/null || true; ip link set $br up; done
  ip addr add 192.168.88.2/24 dev br-mgmt 2>/dev/null || true
  ip -6 addr add 2001:db8:250::2/64 dev br-seg nodad 2>/dev/null || true
  for t in tap0 tap1 tap2 tap3; do ip tuntap add $t mode tap 2>/dev/null || true; ip link set $t up; done
  ip link set tap0 master br-mgmt; ip link set tap1 master br-seg
  ip link set tap2 master br-mgmt; ip link set tap3 master br-seg
  ;;
boot)
  N=$2; T0=$3; T1=$4; MACI=${5:-1}
  [ -f /lab/$N.qcow2 ] || qemu-img create -f qcow2 -F raw -b /lab/chr-$VER-arm64.img /lab/$N.qcow2 >/dev/null
  cp -n /usr/share/AAVMF/AAVMF_VARS.fd /lab/$N.vars.fd 2>/dev/null || true
  exec qemu-system-aarch64 -M virt -cpu ${CPU:-cortex-a72} -smp 2 -m 512 -accel tcg,thread=multi \
    -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd \
    -drive if=pflash,format=raw,file=/lab/$N.vars.fd \
    -drive file=/lab/$N.qcow2,if=none,id=d0,format=qcow2 -device virtio-blk-pci,drive=d0 \
    -netdev tap,id=n0,ifname=$T0,script=no,downscript=no -device virtio-net-pci,netdev=n0,mac=52:54:00:00:0$MACI:01,romfile= \
    -netdev tap,id=n1,ifname=$T1,script=no,downscript=no -device virtio-net-pci,netdev=n1,mac=52:54:00:00:0$MACI:02,romfile= \
    ${EXTRA_NIC:+-netdev user,id=n2 -device virtio-net-pci,netdev=n2,mac=52:54:00:00:0$MACI:03,romfile=} \
    -display none -monitor unix:/lab/$N.mon,server,nowait -chardev socket,id=c0,path=/lab/$N.console,server=on,wait=off,logfile=/lab/$N.serial.log -serial chardev:c0
  ;;
esac
