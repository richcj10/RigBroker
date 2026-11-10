#!/bin/sh
# Turn RigBroker's transmit permission on or off (systemd drop-in, reversible).
# Run ON THE PI:   sudo sh deploy/allow-tx.sh on | off | status
# "on" lets TX-capable consumers run (direwolf@aprs, direwolf@pkt, vara-fm). Every TX is
# still guarded: holdoff, clear channel, read-back verify, stuck-TX (90 s) and
# unexpected-TX watchdogs. Do the first tests into a DUMMY LOAD at low power.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
D=/etc/systemd/system/rigbroker.service.d
F=$D/allow-tx.conf
show() {
  curl -s http://127.0.0.1:8080/status 2>/dev/null | python3 -c '
import json, sys
s = json.load(sys.stdin)
r = s["radios"]["vu"]; t = r["telemetry"]
print("allow_tx:", s.get("allow_tx"), "| vu:", r["state"], r["applied_profile"],
      "| consumer:", r["consumer"], "| online:", t["online"])' 2>/dev/null \
  || echo "broker not answering yet"
}
case "${1:-status}" in
  on)
    install -d -m 0755 "$D"
    cat > "$F" <<'EOF'
# written by deploy/allow-tx.sh - remove with: sudo sh deploy/allow-tx.sh off
[Service]
ExecStart=
ExecStart=/opt/rigbroker/.venv/bin/rigbroker --config /etc/rigbroker/profiles.yaml --log-json --allow-tx
EOF
    ;;
  off) rm -f "$F" ;;
  status) [ -f "$F" ] && echo "drop-in: TX ALLOWED" || echo "drop-in: none (read-only)"; show; exit 0 ;;
  *) echo "usage: sudo sh $0 on|off|status"; exit 2 ;;
esac
systemctl daemon-reload
systemctl restart rigbroker
i=0; while [ $i -lt 30 ] && ! curl -sf http://127.0.0.1:8080/status >/dev/null; do sleep 0.5; i=$((i+1)); done
sleep 6   # let it tune and start the consumer
[ -f "$F" ] && echo "TX ALLOWED" || echo "READ-ONLY (TX off)"
show
