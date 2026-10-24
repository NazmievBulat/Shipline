#!/bin/sh
# Runs inside the container, as its only program: it starts one virtual machine and ends when that
# machine switches itself off. The registration token arrives on standard input and is passed on
# to the machine unread.
set -eu

# This job's disk: the image's disk file with every change written to a file beside it. The file
# is in the container's own storage, so it is gone when the container is.
disk=$HOME/disk.qcow2
qemu-img create -q -f qcow2 -b /vm/disk.img -F raw "$disk" "$VM_DISK"

# -nodefaults: the machine has only the parts named here. No screen, no control channel, no USB.
# The network is QEMU's own (`user`): the machine's connections leave as ordinary connections of
# this container, so the firewall rules for the container hold for the machine too.
{
  printf '%s\n' "${VM_MODE:-job}" "$RUNNER_REPOSITORY" "$RUNNER_NAME" "$RUNNER_LABEL"
  cat
} | qemu-system-x86_64 \
  -nodefaults -no-reboot -display none -monitor none \
  -machine q35,accel=kvm -cpu host -smp "$VM_PROCESSORS" -m "$VM_MEMORY" \
  -kernel /vm/vmlinuz \
  -append "root=/dev/vda rw console=ttyS0 loglevel=3 systemd.show_status=false" \
  -drive "file=$disk,if=virtio,cache=unsafe,discard=unmap" \
  -netdev user,id=net,ipv6=off -device virtio-net-pci,netdev=net \
  -device virtio-rng-pci \
  -device virtio-balloon-pci,free-page-reporting=on \
  -device virtio-serial-pci \
  -chardev stdio,id=job,signal=off -device virtserialport,chardev=job,name=job \
  -serial file:/dev/stderr
