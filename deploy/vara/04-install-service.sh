#!/bin/sh
# VARA step 4: install VARA FM as vara-fm.service (runs as rigbroker, started by RigBroker).
# Run ON THE PI:  sudo sh deploy/vara/04-install-service.sh
# Uses the tested setup from ~ham/vara-test (Wine build + prefix with your VARA settings).
# Re-runnable: updates Wine; keeps an existing service prefix unless --fresh-prefix.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
SRC=/home/ham/vara-test
REPO=$(cd "$(dirname "$0")/../.." && pwd)
VDIR=/var/lib/rigbroker/vara
[ -x "$SRC/wine-x64/bin/wine" ] && [ -f "$SRC/prefix/drive_c/VARA FM/VARAFM.exe" ] \
  || { echo "test install not found in $SRC (steps 2/3 first)"; exit 1; }
pgrep -f 'VARA(FM)?\.exe' >/dev/null \
  && { echo "a VARA is running - stop it first (03-setup-screen.sh stop / systemctl stop vara-fm)"; exit 1; }

echo "== Wine -> /opt/vara/wine (root-owned)"
install -d -m 0755 /opt/vara
rm -rf /opt/vara/wine.new
cp -a "$SRC/wine-x64" /opt/vara/wine.new
chown -R root:root /opt/vara/wine.new
chmod -R go-w /opt/vara/wine.new
rm -rf /opt/vara/wine
mv /opt/vara/wine.new /opt/vara/wine

echo "== VARA prefix (settings) -> $VDIR/prefix (owned by rigbroker)"
install -d -o rigbroker -g rigbroker -m 0700 "$VDIR"
if [ "${1:-}" = "--fresh-prefix" ]; then rm -rf "$VDIR/prefix"; fi
if [ ! -d "$VDIR/prefix" ]; then
  cp -a "$SRC/prefix" "$VDIR/prefix"
  chown -R rigbroker:rigbroker "$VDIR/prefix"
  echo "  copied (with your VARA settings)"
else
  echo "  kept existing service prefix (use --fresh-prefix to replace it)"
fi

echo "== safety: the service user must NOT be able to open the Digirig serial port (PTT)"
for dev in /dev/serial/by-id/*CP2102N* /dev/ttyUSB0; do
  [ -e "$dev" ] || continue
  if sudo -u rigbroker test -w "$dev" || sudo -u rigbroker test -r "$dev"; then
    echo "  DANGER: rigbroker can access $dev - aborting"; exit 1
  fi
  echo "  OK: rigbroker cannot open $dev"
done
id rigbroker | grep -q dialout && { echo "  DANGER: rigbroker is in dialout - aborting"; exit 1; }

echo "== install vara-fm.service (not enabled: RigBroker starts it)"
install -m 0644 "$REPO/deploy/vara-fm.service" /etc/systemd/system/vara-fm.service
systemctl daemon-reload
systemctl reset-failed vara-fm 2>/dev/null || true

echo "== trial start (only if the Digirig audio is free)"
busy=""
for p in /proc/[0-9]*; do ls -l "$p/fd" 2>/dev/null | grep -q 'snd/pcmC2' && busy="$busy $(cat $p/comm)"; done
if [ -n "$busy" ]; then
  echo "  skipped: Digirig audio is in use by:$busy (normal while APRS runs)."
  echo "  Test later: select 'monitor' in RigBroker, then: sudo systemctl start vara-fm"
else
  systemctl start vara-fm
  i=0; while [ $i -lt 90 ] && ! ss -tln | grep -q ':8300 '; do sleep 1; i=$((i+1)); done
  ss -tln | grep -E ':830[01] ' && echo "  VARA FM listening (started in ${i}s)" \
    || { echo "  VARA did not open its ports - journalctl -u vara-fm -n 30"; }
  ps -o user=,pcpu=,args= -C VARAFM.exe 2>/dev/null | cut -c1-80 || true
  t0=$(date +%s)
  systemctl stop vara-fm
  echo "  trial stopped in $(( $(date +%s) - t0 ))s; final state: $(systemctl is-active vara-fm)" \
       "(should be 'inactive', not 'failed')"
fi
echo "Done. RigBroker sees vara-fm as installed within a minute."
