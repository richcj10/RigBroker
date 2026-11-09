#!/bin/sh
# Install RigBroker as a system service in HARDWARE mode, READ-ONLY (no --allow-tx):
# it reads/tunes the ID-4100 via rigctld and runs only receive-only consumers.
#
# Run ON THE PI from the repo:   sudo sh deploy/install-broker.sh
# Safe to re-run (updates code, keeps /etc/rigbroker/profiles.yaml and all state).
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
APP=/opt/rigbroker
STATE=/var/lib/rigbroker
ETC=/etc/rigbroker
HAM=/home/ham

echo "== stop old instances"
systemctl stop rigbroker.service 2>/dev/null || true
pkill -u ham -f -- "-m rigbroker --mock" 2>/dev/null || true   # the dev/mock instance

echo "== service user"
id rigbroker >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --create-home \
  --shell /usr/sbin/nologin rigbroker
usermod -aG audio rigbroker                      # Direwolf runs as rigbroker
install -d -o rigbroker -g rigbroker -m 0700 "$STATE" "$STATE/direwolf"

echo "== code -> $APP (root-owned, read-only for the service)"
mkdir -p "$APP"
rm -rf "$APP/rigbroker" "$APP/web"
cp -a "$SRC/rigbroker" "$SRC/web" "$SRC/pyproject.toml" "$SRC/README.md" \
  "$SRC/requirements.lock" "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.lock"
"$APP/.venv/bin/pip" install -q --no-deps -e "$APP"
chown -R root:root "$APP"
chmod -R go-w "$APP"

echo "== config -> $ETC/profiles.yaml"
install -d -m 0755 "$ETC"
if [ -f "$ETC/profiles.yaml" ]; then
  echo "kept existing $ETC/profiles.yaml (compare with $SRC/config/profiles.yaml)"
else
  sed -e "s#~/.config/rigbroker/#$STATE/#g" -e "s#~/.local/state/rigbroker/#$STATE/#g" \
    "$SRC/config/profiles.yaml" > "$ETC/profiles.yaml"
  chmod 0644 "$ETC/profiles.yaml"
fi
"$APP/.venv/bin/rigbroker" --check-config --config "$ETC/profiles.yaml"

echo "== carry over accounts / settings from the dev instance (only if not already there)"
for pair in ".config/rigbroker/auth.json:auth.json" \
            ".config/rigbroker/initial-admin-password:initial-admin-password" \
            ".local/state/rigbroker/winlink.json:winlink.json" \
            ".local/state/rigbroker/direwolf-settings.json:direwolf-settings.json"; do
  from="$HAM/${pair%%:*}"; to="$STATE/${pair##*:}"
  if [ -f "$from" ] && [ ! -f "$to" ]; then
    install -o rigbroker -g rigbroker -m 0600 "$from" "$to"
    echo "  copied $from"
  fi
done

echo "== systemd units + polkit rule"
install -m 0644 "$SRC/deploy/rigbroker.service" /etc/systemd/system/rigbroker.service
install -m 0644 "$SRC/deploy/direwolf@.service" /etc/systemd/system/direwolf@.service
install -m 0644 "$SRC/deploy/audio-stream.service" /etc/systemd/system/audio-stream.service
install -m 0644 "$SRC/deploy/50-rigbroker.rules" /etc/polkit-1/rules.d/50-rigbroker.rules
systemctl restart polkit
systemctl daemon-reload

echo "== helper commands"
cat > /usr/local/bin/rigbroker-admin <<'EOF'
#!/bin/sh
# Account admin for the installed broker (runs as the rigbroker service user).
exec sudo -u rigbroker /opt/rigbroker/.venv/bin/rigbroker-admin --config /etc/rigbroker/profiles.yaml "$@"
EOF
chmod 0755 /usr/local/bin/rigbroker-admin
ln -sf "$APP/.venv/bin/rigbrokerctl" /usr/local/bin/rigbrokerctl

echo "== start"
systemctl enable rigbroker.service
systemctl restart rigbroker.service
i=0
while [ $i -lt 30 ] && ! ss -tln | grep -q ':8080 '; do sleep 0.5; i=$((i+1)); done
systemctl --no-pager is-active rigbroker.service
curl -s http://127.0.0.1:8080/status | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d["radios"]["vu"]; print("mode:", "MOCK" if d["mock"] else "HARDWARE", "| allow_tx:", d["allow_tx"], "| vu:", r["state"], "| rig online:", r["telemetry"]["online"])' \
  || { echo "not answering yet - see: journalctl -u rigbroker -n 40"; exit 1; }
echo "Web UI: http://$(hostname -I | cut -d' ' -f1):8080/"
