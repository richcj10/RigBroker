#!/bin/sh
# VARA FM on the Pi 5 - step 2 of 3: x86-64 Wine (under box64) + VARA FM, TEST INSTALL.
# Run ON THE PI as ham (NO sudo):   sh deploy/vara/02-wine-vara.sh
#
# Everything lands in ~/vara-test (remove that folder to undo). Nothing here touches the
# running RigBroker or the radio. Each stage is skipped if already done, so re-run freely.
#
# Before running: put the VARA FM installer zip from the official site (rosmodem.com ->
# downloads.winlink.org "VARA Products") in ~/Downloads on the Pi, e.g. from Windows:
#   scp "%USERPROFILE%\Downloads\VARA FM v4.x.x setup.zip" ham@192.168.5.157:Downloads/
set -eu
[ "$(id -u)" -ne 0 ] || { echo "run as ham, not root"; exit 1; }
[ "$(getconf PAGESIZE)" = 4096 ] || { echo "need the 4K-page kernel (step 1 + reboot)"; exit 1; }
command -v box64 >/dev/null || { echo "box64 missing (step 1)"; exit 1; }

T="$HOME/vara-test"
WINE_VER=10.0
WINE_URL="https://github.com/Kron4ek/Wine-Builds/releases/download/$WINE_VER/wine-$WINE_VER-amd64-wow64.tar.xz"
WT_URL="https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"
export WINEPREFIX="$T/prefix" WINEDEBUG=-all BOX64_LOG=0 BOX64_NOBANNER=1
mkdir -p "$T"
step() { echo; echo "== $*"; }

step "1/5 x86-64 Wine $WINE_VER (new WoW64: runs 32-bit VARA without 32-bit host libs)"
W64="$T/wine-x64"
# earlier version of this script unpacked to $T/wine: reuse it
if [ ! -x "$W64/bin/wine" ] && [ -x "$T/wine/bin/wine" ]; then mv "$T/wine" "$W64"; fi
if [ ! -x "$W64/bin/wine" ]; then
  curl -fL --retry 2 -o "$T/wine.tar.xz" "$WINE_URL" \
    || { echo "download failed: $WINE_URL"; exit 1; }
  mkdir -p "$W64"
  tar -xJf "$T/wine.tar.xz" -C "$W64" --strip-components=1
  rm -f "$T/wine.tar.xz"
fi
# wrappers so Wine tools (and winetricks) always run through box64
mkdir -p "$T/bin"
# (winetricks uses "wine64" next to $WINE for 64-bit prefixes, so wrap that too)
for tool in wine wine64 wineserver; do
  printf '#!/bin/sh\nexec box64 "%s/bin/%s" "$@"\n' "$W64" "$tool" > "$T/bin/$tool"
  chmod +x "$T/bin/$tool"
done
export WINE="$T/bin/wine" WINESERVER="$T/bin/wineserver"
"$WINE" --version || { echo "box64 could not run Wine"; exit 1; }

step "2/5 Wine prefix (first start is slow under emulation - a few minutes is normal)"
# Skip Wine Mono/Gecko: VARA needs neither, and their install prompt would sit forever on
# the invisible Xvfb screen (that's what hung the first attempt).
export WINEDLLOVERRIDES="mscoree=;mshtml="
if [ ! -f "$T/.prefix-done" ]; then
  rm -rf "$WINEPREFIX"   # never build on top of a half-made prefix
  xvfb-run -a "$WINE" wineboot -i
  "$WINESERVER" -w
  # sanity: Windows must answer basic questions before we install anything into it
  APPDATA=$("$WINE" cmd.exe /c echo %AppData% 2>/dev/null | tr -d '\r')
  case "$APPDATA" in
    *AppData*) echo "prefix OK (AppData = $APPDATA)" ;;
    *) echo "prefix check failed (AppData='$APPDATA') - paste this to Claude"; exit 1 ;;
  esac
  "$WINESERVER" -w
  touch "$T/.prefix-done"
fi
echo "prefix: $WINEPREFIX"

step "3/5 VB6 runtime + PDH (VARA needs them) via winetricks"
[ -f "$T/winetricks" ] || curl -fL -o "$T/winetricks" "$WT_URL"
if [ ! -f "$T/.winetricks-done" ]; then
  xvfb-run -a sh "$T/winetricks" -q vb6run pdh_nt4
  "$WINESERVER" -w
  touch "$T/.winetricks-done"
fi

step "4/5 VARA FM installer"
ZIP=$(ls -t "$HOME"/Downloads/*[Vv][Aa][Rr][Aa]*[Ff][Mm]*.zip "$HOME"/*[Vv][Aa][Rr][Aa]*[Ff][Mm]*.zip \
      2>/dev/null | head -1 || true)
EXE=$(find "$WINEPREFIX/drive_c" -iname 'VARAFM.exe' 2>/dev/null | head -1)
if [ -z "$EXE" ]; then
  [ -n "$ZIP" ] || { echo "No VARA FM zip found in ~/Downloads - copy it there and re-run."; exit 2; }
  echo "using $ZIP"
  rm -rf "$T/vara-setup"; mkdir -p "$T/vara-setup"
  unzip -q -o "$ZIP" -d "$T/vara-setup"
  SETUP=$(find "$T/vara-setup" -iname '*.exe' | head -1)
  [ -n "$SETUP" ] || { echo "no .exe inside $ZIP"; exit 1; }
  xvfb-run -a "$WINE" "$SETUP" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART &
  SETUP_PID=$!
  # VARA's helper ends with an invisible "VARA installed successfully" box ("VARA Setup"
  # window): once it appears the work is done, so close it instead of waiting forever.
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
  EXE=$(find "$WINEPREFIX/drive_c" -iname 'VARAFM.exe' | head -1)
fi
[ -n "$EXE" ] || { echo "VARAFM.exe not found after install"; exit 1; }
echo "VARA FM: $EXE"

step "5/5 smoke test: start VARA FM headless for 60 s, check its TCP ports and CPU"
xvfb-run -a "$WINE" "$EXE" >/dev/null 2>&1 &
sleep 45
echo "listening:"; ss -tln | grep -E ':830[01] ' || echo "  (VARA ports 8300/8301 not open yet)"
ps -eo pcpu,rss,args --sort=-pcpu | grep -iE 'varafm|box64' | grep -v grep | head -3 | cut -c1-110
sleep 15
"$WINESERVER" -k 2>/dev/null || true
echo
echo "Step 2 done. Tell Claude the output; step 3 = sound-card setup over VNC + performance test."
