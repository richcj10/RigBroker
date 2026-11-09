#!/bin/sh
# Remote Winlink: Winlink Express on the PC drives the Pi's modems over the LAN.
# Run ON THE PI from the repo:   sudo sh deploy/install-remote-winlink.sh
#  1. remove Pat (Winlink doesn't run on the Pi)
#  2. VARA FM PTT bridge: socat virtual COM pair + rigctlcom (TS-2000) -> rigctld
#  3. map Wine COM10 in the service VARA prefix to the virtual COM
#  4. firewall: open 8010 (packet KISS) + 8300/8301 (VARA) to the Winlink PC ONLY
#  5. refresh /etc/rigbroker/profiles.yaml + reinstall RigBroker (remote Winlink sessions)
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
RB=/var/lib/rigbroker

echo "== 1. remove Pat"
if dpkg -s pat >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq pat >/dev/null && echo "  removed"
else
  echo "  not installed"
fi
rm -rf "$RB/.config/pat" "$RB/.local/share/pat" "$RB/.local/state/pat"

echo "== 2. VARA PTT bridge (socat + rigctlcom -> rigctld)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq socat >/dev/null
install -d -m 0755 /usr/local/lib/rigbroker
install -m 0755 "$REPO/deploy/vara-cat.sh" /usr/local/lib/rigbroker/vara-cat.sh
install -m 0644 "$REPO/deploy/vara-cat.service" /etc/systemd/system/vara-cat.service
install -m 0644 "$REPO/deploy/vara-fm.service" /etc/systemd/system/vara-fm.service
systemctl daemon-reload
systemctl start vara-cat
sleep 2
if systemctl is-active --quiet vara-cat && [ -e /run/rigbroker-cat/vara ]; then
  echo "  bridge OK: /run/rigbroker-cat/vara -> $(readlink /run/rigbroker-cat/vara)"
else
  echo "  bridge FAILED:"; journalctl -u vara-cat -n 15 --no-pager -o cat; exit 1
fi
systemctl stop vara-cat

echo "== 3. Wine COM10 -> virtual COM (service VARA prefix)"
sudo -u rigbroker env HOME="$RB" WINEPREFIX="$RB/vara/prefix" WINEDEBUG=-all BOX64_LOG=0 \
  BOX64_NOBANNER=1 "WINEDLLOVERRIDES=mscoree=;mshtml=" \
  sh -c 'box64 /opt/vara/wine/bin/wine reg add "HKLM\\Software\\Wine\\Ports" /v COM10 /t REG_SZ /d /run/rigbroker-cat/vara /f >/dev/null 2>&1; box64 /opt/vara/wine/bin/wineserver -w'
sudo -u rigbroker env WINEPREFIX="$RB/vara/prefix" WINEDEBUG=-all BOX64_LOG=0 BOX64_NOBANNER=1 \
  box64 /opt/vara/wine/bin/wine reg query 'HKLM\Software\Wine\Ports' 2>/dev/null | tr -d '\r' | grep -E 'COM(8|10)' | sed 's/^/  /'

echo "== 4. firewall: Winlink ports to the Winlink PC only"
nft -c -f "$REPO/deploy/nftables.conf"
cp -a /etc/nftables.conf /etc/nftables.conf.pre-winlink
cp "$REPO/deploy/nftables.conf" /etc/nftables.conf
systemctl restart nftables
nft list chain inet hamcontrol input | grep -E '8010|8300' | sed 's/^ */  /'

echo "== 5. RigBroker: remote Winlink sessions"
cp -a /etc/rigbroker/profiles.yaml "/etc/rigbroker/profiles.yaml.pre-winlink-$(date +%s)"
sed -e "s#~/.config/rigbroker/#$RB/#g" -e "s#~/.local/state/rigbroker/#$RB/#g" \
  "$REPO/config/profiles.yaml" > /etc/rigbroker/profiles.yaml
sh "$REPO/deploy/install-broker.sh"

echo
echo "Next: set VARA's PTT to CAT/COM10 in the service copy:"
echo "  sudo sh deploy/vara/05-service-setup-screen.sh start   (then VNC to localhost:5950)"
