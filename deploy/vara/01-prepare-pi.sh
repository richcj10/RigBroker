#!/bin/sh
# VARA FM on the Pi 5 - step 1 of 3: 4K-page kernel + Debian packages.
# Run ON THE PI:  sudo sh deploy/vara/01-prepare-pi.sh   then:  sudo reboot
#
# Why: VARA is a 32-bit Windows x86 program. On ARM64 it needs box64 (x86-64 emulator)
# under an x86-64 Wine; both need 4 KB memory pages, but the Pi 5's default kernel
# (kernel_2712.img) uses 16 KB. kernel8.img is the stock 4 KB-page kernel.
#
# Undo: sudo cp /boot/firmware/config.txt.pre-vara /boot/firmware/config.txt && sudo reboot
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
CFG=/boot/firmware/config.txt

echo "== kernel: 4K pages"
[ -f /boot/firmware/kernel8.img ] || { echo "kernel8.img missing - aborting"; exit 1; }
if grep -qE '^kernel=kernel8\.img' "$CFG"; then
  echo "already set"
else
  [ -f "$CFG.pre-vara" ] || cp -a "$CFG" "$CFG.pre-vara"
  printf '\n[all]\n# RigBroker/VARA: 4K-page kernel for box64 + Wine\nkernel=kernel8.img\n' >> "$CFG"
  echo "added kernel=kernel8.img (backup: $CFG.pre-vara)"
fi

echo "== packages (Debian): box64, Xvfb, x11vnc for the one-time VARA setup screen"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq box64 xvfb x11vnc cabextract unzip \
  >/dev/null
dpkg-query -W -f='  ${Package} ${Version}\n' box64 xvfb x11vnc

echo
echo "Current page size: $(getconf PAGESIZE) (becomes 4096 after reboot)"
echo "Next: sudo reboot   - then tell Claude; step 2 installs x86-64 Wine + VARA."
