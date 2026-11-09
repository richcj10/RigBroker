#!/bin/sh
# Install + start rigctld for the ID-4100A. Run ON THE PI:  sudo sh deploy/install-rigctld.sh
# Best done with NO radio attached (or the radio off): each rigctld start pulses the
# Digirig's RTS/PTT for ~55 ms. Once running, it stays up, so connecting the radio later
# doesn't key it. Safe to re-run.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"
command -v rigctld >/dev/null || { echo "rigctld missing: apt-get install libhamlib-utils"; exit 1; }

install -m 0644 rigctld-vu.service /etc/systemd/system/rigctld-vu.service
systemctl daemon-reload
systemctl enable rigctld-vu.service
systemctl restart rigctld-vu.service

echo "waiting for rigctld to listen on 127.0.0.1:4542 ..."
i=0
while [ $i -lt 20 ] && ! ss -tln | grep -q '127.0.0.1:4542 '; do sleep 0.5; i=$((i+1)); done
echo
systemctl --no-pager is-enabled rigctld-vu.service
systemctl --no-pager is-active rigctld-vu.service
ss -tln | grep '127.0.0.1:4542 ' && echo "rigctld is listening (localhost only)" \
  || echo "WARNING: rigctld not listening yet - see: journalctl -u rigctld-vu -n 30"
