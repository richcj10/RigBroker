#!/bin/sh
# Back up the Pi's LIVE RigBroker state (not in git: accounts, live config, Winlink
# history, VARA settings, installed system files). Run ON THE PI:
#   sudo sh deploy/backup-pi.sh            -> /var/backups/rigbroker/hamcontrol-<date>.tar.gz
# The archive is readable only by root and ham (so it can be copied off over SSH).
# It contains password HASHES and API token hashes: never put it on GitHub.
# Restore: sudo tar -xzpf <archive> -C /   (then: sudo systemctl daemon-reload;
#          sudo systemctl restart rigbroker)
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
OUT=/var/backups/rigbroker
KEEP=10
STAMP=$(date +%Y%m%d-%H%M%S)
FILE="$OUT/hamcontrol-$STAMP.tar.gz"
install -d -m 0750 -o root -g ham "$OUT"

LIST=$(mktemp); trap 'rm -f "$LIST"' EXIT
add() { for p in "$@"; do [ -e "$p" ] && echo "${p#/}" >> "$LIST"; done; return 0; }

# RigBroker config + state (accounts, tokens, Winlink favorites/history, Direwolf settings)
add /etc/rigbroker
for f in /var/lib/rigbroker/*.json /var/lib/rigbroker/direwolf; do add "$f"; done
# VARA: settings only (registry + VARA .ini files), not the whole Wine prefix
P=/var/lib/rigbroker/vara/prefix
add "$P/system.reg" "$P/user.reg" "$P/userdef.reg"
find "$P/drive_c" -maxdepth 2 -path '*VARA*' -name '*.ini' 2>/dev/null | while read -r f; do add "$f"; done
# installed system pieces
add /etc/nftables.conf /etc/ssh/sshd_config.d /etc/polkit-1/rules.d/50-rigbroker.rules
for f in /etc/systemd/system/rigbroker.service* /etc/systemd/system/rigctld-vu.service \
         /etc/systemd/system/direwolf@.service /etc/systemd/system/audio-stream.service \
         /etc/systemd/system/vara-fm.service /etc/systemd/system/vara-cat.service \
         /usr/local/lib/rigbroker /usr/local/bin/rigbroker-admin; do add "$f"; done

# what code version was running
( cd /opt/rigbroker 2>/dev/null && ls -la --time-style=+%F_%T rigbroker | head -3 ) \
  > /etc/rigbroker/.backup-info 2>/dev/null || true
echo "backup $STAMP host $(hostname)" >> /etc/rigbroker/.backup-info

tar -czpf "$FILE" -C / -T "$LIST"
chown root:ham "$FILE"; chmod 0640 "$FILE"
# keep the newest $KEEP
ls -1t "$OUT"/hamcontrol-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "$FILE ($(du -h "$FILE" | cut -f1), $(tar -tzf "$FILE" | wc -l) entries)"
