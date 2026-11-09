#!/bin/bash
# VARA FM CAT PTT bridge (run by vara-cat.service as rigbroker):
#   VARA (Wine COM10) -> /run/rigbroker-cat/vara  <-pty pair->  /run/rigbroker-cat/rig
#   -> rigctlcom (Kenwood TS-2000 emulator) -> rigctld 127.0.0.1:4542 -> Digirig RTS (PTT)
# rigctld stays the only program on the Digirig serial port, so RigBroker sees every PTT
# (stuck-TX watchdog) and VARA never touches the serial port itself.
set -euo pipefail
D=/run/rigbroker-cat
socat pty,raw,echo=0,link="$D/vara",mode=600 pty,raw,echo=0,link="$D/rig",mode=600 &
SOCAT=$!
trap 'kill "$SOCAT" 2>/dev/null || true' EXIT INT TERM
for _ in $(seq 50); do [ -e "$D/rig" ] && break; sleep 0.1; done
[ -e "$D/rig" ] || { echo "virtual COM pair did not appear"; exit 1; }
rigctlcom -m 2 -r 127.0.0.1:4542 -R "$D/rig" -S 9600 &
RIGCOM=$!
wait -n "$SOCAT" "$RIGCOM"   # if either side dies, stop (systemd restarts with VARA)
exit 1
