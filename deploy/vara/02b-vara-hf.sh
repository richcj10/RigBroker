#!/bin/sh
# VARA HF on the Pi 5 - step 2b: add VARA HF to the SAME Wine prefix as VARA FM.
# Run AFTER 02-wine-vara.sh has finished, as ham (NO sudo):  sh deploy/vara/02b-vara-hf.sh
#
# Needs the VARA HF installer zip (official: rosmodem.com -> downloads.winlink.org
# "VARA Products") in ~/Downloads, e.g. from Windows:
#   scp "%USERPROFILE%\Downloads\VARA HF v4*.zip" ham@192.168.5.157:Downloads/
#
# Note: VARA HF and VARA FM both default to TCP 8300/8301. They're for different radios
# (HF = FT-891 + its own Digirig), so before running both at once, set VARA HF to
# 8310/8311 in its settings (done in step 3 over VNC).
set -eu
[ "$(id -u)" -ne 0 ] || { echo "run as ham, not root"; exit 1; }
T="$HOME/vara-test"
export WINEPREFIX="$T/prefix" WINEDEBUG=-all BOX64_LOG=0 BOX64_NOBANNER=1
export WINE="$T/bin/wine" WINESERVER="$T/bin/wineserver"
[ -x "$WINE" ] && [ -f "$WINEPREFIX/system.reg" ] && [ -f "$T/.winetricks-done" ] \
  || { echo "run deploy/vara/02-wine-vara.sh to completion first"; exit 1; }
pgrep -f 'VARAFM.exe' >/dev/null && { echo "VARA FM is running - wait for step 2 to finish"; exit 1; }

echo "== VARA HF installer"
# HF zip: has "VARA" and "HF" in the name (never the FM one)
ZIP=$(ls -t "$HOME"/Downloads/*[Vv][Aa][Rr][Aa]*[Hh][Ff]*.zip "$HOME"/*[Vv][Aa][Rr][Aa]*[Hh][Ff]*.zip \
      2>/dev/null | head -1 || true)
EXE=$(find "$WINEPREFIX/drive_c" -iname 'VARA.exe' 2>/dev/null | head -1)   # HF = VARA.exe
if [ -z "$EXE" ]; then
  [ -n "$ZIP" ] || { echo "No VARA HF zip found in ~/Downloads - copy it there and re-run."; exit 2; }
  echo "using $ZIP"
  rm -rf "$T/varahf-setup"; mkdir -p "$T/varahf-setup"
  unzip -q -o "$ZIP" -d "$T/varahf-setup"
  SETUP=$(find "$T/varahf-setup" -iname '*.exe' | head -1)
  [ -n "$SETUP" ] || { echo "no .exe inside $ZIP"; exit 1; }
  xvfb-run -a "$WINE" "$SETUP" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART &
  SETUP_PID=$!
  # VARA's own helper ("VARA Install.exe") ends with a "VARA installed successfully" box
  # on the invisible screen. When that box ("VARA Setup" window) appears, the work is
  # done: close the helper instead of waiting forever for an OK click.
  i=0
  while kill -0 "$SETUP_PID" 2>/dev/null && [ $i -lt 600 ]; do
    D=$(ps -eo args | grep -o '[X]vfb :[0-9]*' | awk '{print $2}' | head -1)
    A=$(ls -d /tmp/xvfb-run.*/Xauthority 2>/dev/null | head -1)
    if [ -n "$D" ] && XAUTHORITY="$A" xwininfo -display "$D" -root -tree 2>/dev/null \
         | grep -q '"VARA Setup": ("vara install.exe"'; then
      echo "VARA reported 'installed successfully' - closing its message box"
      pkill -u "$(id -u)" -f 'VARA Install.exe' || true
    fi
    sleep 2; i=$((i+2))
  done
  wait "$SETUP_PID" 2>/dev/null || true
  "$WINESERVER" -w
  EXE=$(find "$WINEPREFIX/drive_c" -iname 'VARA.exe' | head -1)
fi
[ -n "$EXE" ] || { echo "VARA.exe (HF) not found after install"; exit 1; }
echo "VARA HF: $EXE"
echo "VARA FM: $(find "$WINEPREFIX/drive_c" -iname 'VARAFM.exe' | head -1)"

echo "== smoke test: VARA HF headless for 60 s (FM is not running, so 8300/8301 are free)"
xvfb-run -a "$WINE" "$EXE" >/dev/null 2>&1 &
sleep 45
echo "listening:"; ss -tln | grep -E ':830[01] ' || echo "  (8300/8301 not open yet)"
ps -eo pcpu,rss,args --sort=-pcpu | grep -iE 'vara\.exe|box64' | grep -v grep | head -3 | cut -c1-110
sleep 15
"$WINESERVER" -k 2>/dev/null || true
echo
echo "Step 2b done. Paste the output to Claude."
