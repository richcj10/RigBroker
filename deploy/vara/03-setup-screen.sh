#!/bin/sh
# VARA step 3: a temporary screen you can see over VNC, for VARA's one-time setup.
# Run ON THE PI as ham (NO sudo):
#   sh deploy/vara/03-setup-screen.sh start fm    VARA FM on the setup screen
#   sh deploy/vara/03-setup-screen.sh start hf    VARA HF on the setup screen
#   sh deploy/vara/03-setup-screen.sh stop        close everything (settings are saved)
#   sh deploy/vara/03-setup-screen.sh cpu         CPU/memory of the running VARA
#
# The VNC server listens on 127.0.0.1:5950 ONLY (the firewall blocks it anyway). Reach it
# through an SSH tunnel from your PC:
#   ssh -N -L 5950:127.0.0.1:5950 ham@192.168.5.157      (leave this window open)
#   then point a VNC viewer (TightVNC / RealVNC / UltraVNC) at  localhost:5950
set -eu
T="$HOME/vara-test"
export WINEPREFIX="$T/prefix" WINEDEBUG=-all BOX64_LOG=0 BOX64_NOBANNER=1
export WINE="$T/bin/wine" WINESERVER="$T/bin/wineserver"
DISP=:50
RUN="$T/run"
mkdir -p "$RUN"

stop() {
  for p in vara x11vnc xvfb; do
    [ -f "$RUN/$p.pid" ] && kill "$(cat "$RUN/$p.pid")" 2>/dev/null || true
    rm -f "$RUN/$p.pid"
  done
  "$WINESERVER" -k 2>/dev/null || true
  echo "setup screen stopped (VARA settings are saved in the Wine prefix)"
}

case "${1:-}" in
  start)
    case "${2:-}" in
      fm) EXE="$WINEPREFIX/drive_c/VARA FM/VARAFM.exe" ;;
      hf) EXE="$WINEPREFIX/drive_c/VARA/VARA.exe" ;;
      *) echo "usage: $0 start fm|hf"; exit 2 ;;
    esac
    [ -f "$EXE" ] || { echo "not installed: $EXE (run step 2/2b)"; exit 1; }
    stop >/dev/null 2>&1 || true
    Xvfb "$DISP" -screen 0 1280x900x24 -nolisten tcp >"$RUN/xvfb.log" 2>&1 &
    echo $! > "$RUN/xvfb.pid"
    sleep 1
    x11vnc -display "$DISP" -localhost -rfbport 5950 -forever -shared -nopw -quiet \
      >"$RUN/x11vnc.log" 2>&1 &
    echo $! > "$RUN/x11vnc.pid"
    DISPLAY="$DISP" "$WINE" "$EXE" >"$RUN/vara.log" 2>&1 &
    echo $! > "$RUN/vara.pid"
    sleep 8
    ss -tln | grep -q '127.0.0.1:5950 ' && echo "VNC ready on 127.0.0.1:5950 (VARA ${2} starting)" \
      || { echo "VNC did not start:"; tail -5 "$RUN/x11vnc.log"; exit 1; }
    ;;
  stop) stop ;;
  cpu)
    ps -eo pcpu,rss,etime,args --sort=-pcpu | grep -iE '[V]ARA(FM)?\.exe' | cut -c1-110 \
      || echo "VARA not running"
    ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
