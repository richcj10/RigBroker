#!/bin/sh
# Setup screen for the SERVICE copy of VARA FM (the one RigBroker runs), e.g. to set PTT.
# Run ON THE PI:  sudo sh deploy/vara/05-service-setup-screen.sh start | stop
# Same VNC tunnel as step 3:  ssh -N -L 5950:127.0.0.1:5950 ham@192.168.5.157  -> localhost:5950
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
RB=/var/lib/rigbroker
RUN=/run/rigbroker-setup
as_rb() { sudo -u rigbroker env HOME="$RB" WINEPREFIX="$RB/vara/prefix" WINEDEBUG=-all \
  BOX64_LOG=0 BOX64_NOBANNER=1 "WINEDLLOVERRIDES=mscoree=;mshtml=" DISPLAY=:51 "$@"; }

stop() {
  # ask VARA to close like clicking X, so it saves its settings; then force what's left
  if pgrep -u rigbroker -f 'VARAFM\.exe' >/dev/null; then
    as_rb box64 /opt/vara/wine/bin/wine taskkill /IM VARAFM.exe >/dev/null 2>&1 || true
    i=0; while pgrep -u rigbroker -f 'VARAFM\.exe' >/dev/null && [ $i -lt 10 ]; do sleep 1; i=$((i+1)); done
  fi
  for p in vara x11vnc xvfb; do
    [ -f "$RUN/$p.pid" ] && kill "$(cat "$RUN/$p.pid")" 2>/dev/null || true
  done
  as_rb box64 /opt/vara/wine/bin/wineserver -k 2>/dev/null || true
  rm -rf "$RUN"
  systemctl stop vara-cat 2>/dev/null || true
  echo "service setup screen stopped (settings saved in $RB/vara/prefix)"
}

case "${1:-}" in
  start)
    systemctl is-active --quiet vara-fm && { echo "vara-fm is running - Release it in RigBroker first"; exit 1; }
    # VARA only looks for sound cards when it starts: the Digirig must be free by then
    for u in direwolf@aprs direwolf@aprs-rx direwolf@pkt; do
      if systemctl is-active --quiet "$u"; then
        echo "The Digirig sound card is in use by $u."
        echo "Select 'monitor' in RigBroker first (it stops Direwolf), then run this again."
        exit 1
      fi
    done
    stop >/dev/null 2>&1 || true
    install -d -o rigbroker -g rigbroker -m 0700 "$RUN"
    systemctl start vara-cat   # virtual COM10 for PTT (bridged to rigctld)
    sudo -u rigbroker Xvfb :51 -screen 0 1280x900x24 -nolisten tcp >/dev/null 2>&1 &
    echo $! > "$RUN/xvfb.pid"; sleep 1
    sudo -u rigbroker x11vnc -display :51 -localhost -rfbport 5950 -forever -shared -nopw -quiet \
      >/dev/null 2>&1 &
    echo $! > "$RUN/x11vnc.pid"
    as_rb box64 /opt/vara/wine/bin/wine "C:\\VARA FM\\VARAFM.exe" >/dev/null 2>&1 &
    echo $! > "$RUN/vara.pid"
    sleep 10
    echo "VNC ready on 127.0.0.1:5950 - this is the SERVICE copy of VARA FM"
    echo "Set: Settings -> PTT: CAT, rig Kenwood TS-2000, port COM10, 9600 baud"
    ;;
  stop) stop ;;
  *) echo "usage: sudo sh $0 start|stop"; exit 2 ;;
esac
