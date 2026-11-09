# RigBroker

Single owner of the station's radios. Decides which job (APRS, Winlink packet, VARA FM,
monitor, off) uses each radio, switches the rig safely over CI-V via Hamlib `rigctld`,
starts/stops the matching audio software, and reports status.

**Status: milestone 2.** Core (M1) + REST API, WebSocket live updates, web UI, `rigbrokerctl`
CLI, and a mock-mode activity simulator with fault injection. MQTT, systemd and the
scheduler follow.

## Quick start (on the Pi)

```bash
cd ~/rigbroker
make venv      # private virtualenv; never touches system Python
make check     # ruff + mypy --strict + full pytest suite
make mock      # broker + web UI in mock mode on :8080 (Ctrl-C to stop)
scripts/mock-bg.sh start|stop|status|log   # same, in the background
.venv/bin/rigbroker --check-config --config config/profiles.yaml
```

Open `http://<pi>:8080/`. In mock mode the "Mock fault injection" panel on each radio card
can hold PTT on (stuck TX), take the rig offline, corrupt read-back, hold the channel busy
or turn the VFO knob, so every safety path can be exercised from the browser.

## REST API

| Method | Path | Notes |
|---|---|---|
| GET | `/status` | all radios |
| GET | `/radios/{id}` | one radio |
| POST | `/radios/{id}/lease` | `{profile, minutes?, priority: manual\|scheduled, holder, queue}` → 200 granted/preempted, 202 queued, 409 conflict + `reason`, 422 invalid |
| POST | `/radios/{id}/release` | 409 if no lease |
| POST | `/radios/{id}/winlink/check` | leases the radio's `winlink_profile` |
| POST | `/radios/{id}/fault/clear` | only if rig answers and is in RX |
| GET | `/events?limit=&radio=&debug=` | event log |
| WS | `/ws` | `{type: status\|events\|event, data}` pushes |
| POST | `/mock/{id}/inject` | mock mode only: `{fault: stuck_tx\|release_tx\|offline\|online\|readback_bad\|readback_ok\|busy\|clear\|knob}` |

Interactive docs: `http://<pi>:8080/docs`.

## Security

**Who can do what:** anyone on the LAN can *view* (status, events, gateways, APRS). Changing
anything needs a **user account** (log in at `/login`) or an **API token**
(`Authorization: Bearer rb_...`). Roles: **operator** runs the radio (leases, Winlink, APRS
messages, clearing faults); **admin** also manages users (`/account`), Direwolf settings
and the gateway list. Lease holders and the audit log record the real username.

**First start:** the broker creates user `admin` with a random one-time password in
`~/.config/rigbroker/initial-admin-password` (mode 0600). Log in with it at `/login`; you
must choose your own password before anything else works, and the file is then deleted.
New users and admin password resets work the same way (one-time password, forced change).

```bash
ssh ham@<pi> cat ~/.config/rigbroker/initial-admin-password   # first login only
.venv/bin/rigbroker-admin user list | user add NAME [--role admin] | user reset NAME
.venv/bin/rigbroker-admin passwd [USER]          # set a password directly (lock-out recovery)
.venv/bin/rigbroker-admin token create home-assistant [--role admin]   # shown once
.venv/bin/rigbroker-admin token list | token revoke NAME | logout-all | status
```

| Protection | How |
|---|---|
| Password storage | scrypt (n=2^15), file mode 0600 in a 0700 dir; changes picked up live |
| Brute force | lockout after 5 failures for 5 min per address; scrypt makes guesses slow |
| CSRF | session writes need the `X-RigBroker: 1` header (forces a CORS preflight we never grant) **and** a same-origin `Origin` |
| WebSocket hijacking | cross-origin WebSocket handshakes refused |
| XSS / clickjacking | strict CSP (`script-src 'self'`, no inline), `X-Frame-Options: DENY`, `nosniff`, all user data rendered with `textContent` |
| Spoofed holders | with a token, the lease holder **is** the token name |
| Audit | every control request, login and failed login is an `audit` event (who, how, from where) |
| Command injection | gateway callsigns strictly validated before they reach `pat connect` |

**Pi hardening** (configs in `deploy/`, applied 2026-11-08):

* `nftables.conf`: inbound allows only SSH and 8080 from 192.168.5.0/24, plus ICMP, DHCP and mDNS.
  Direwolf (8000/8001), VARA (8300/8301) and rigctld ports are **never** reachable from the
  network, since they can key the transmitter around the broker.
* `sshd-hardening.conf`: keys only, no root, no X11, `AllowUsers ham`, 3 tries.
* `rpcbind` disabled/masked (no NFS in use); `unattended-upgrades` for Debian security updates.
* `50-rigbroker.rules` + `rigbroker.service`: the broker will run as the unprivileged
  `rigbroker` user (systemd sandboxing) and may start/stop **only** its own units via polkit,
  with no sudo (service installed in milestone 4).

Not done (by choice): HTTPS and remote access. Don't port-forward 8080; use WireGuard on
the router when remote access is wanted.

## APRS tab (`http://<pi>:8080/#aprs`)

A separate view (bookmarkable; open it in its own browser tab) for station **KY4WL-5**:

* **Map**: live Leaflet/OpenStreetMap map of stations heard on RF (orange = moving,
  purple = digi/iGate, blue = weather, green = fixed); click for details + aprs.fi link.
* **Messages**: send (≤67 chars, needs login) with message IDs, retries (30 s, 60 s, ...)
  and ✓ delivered on ack; incoming messages to KY4WL-5 are **acked automatically**, those
  to KY4WL are shown but not acked (your other radio does that). Bulletins too.
* **Heard**: distance, bearing, last heard, via which digipeater. **Packets**: RX/TX log.
* **Gating**: APRS only listens while `direwolf@aprs`/`aprs-rx` owns the radio, and only
  transmits while `direwolf@aprs` runs (so never on the Winlink frequency, never in quiet
  hours, never without `--allow-tx`).

**Direwolf** is configured by the broker: edit settings on the APRS tab → *Save & apply*.
The broker validates every value (no config injection), writes `aprs.conf`,
`aprs-rx.conf` (no PTT at all) and `pkt.conf` (AGW for Pat, no beacons) to
`~/.local/state/rigbroker/direwolf/` (mode 0600, holds the iGate passcode), and restarts
the running Direwolf only after the rig is in RX. Install once on the Pi:

```bash
sudo sh deploy/install-direwolf.sh     # apt package + direwolf@.service template
```

## Winlink from your PC (Winlink Express)

Winlink does **not** run on the Pi. Winlink Express on the PC connects to the Pi's modems
over the LAN; RigBroker tunes the radio, starts the modem and holds the radio for the
session (Release or `max_minutes` ends it). Step-by-step instructions are on the web page
(**Connect Winlink Express** panel, Station view).

| Mode | Winlink Express setting | Pi side |
|---|---|---|
| Packet | KISS over TCP/IP, `<pi>:8010` | `direwolf@pkt` (own KISS port, not the APRS one) |
| VARA FM | VARA TCP address `<pi>`, port 8300 | `vara-fm.service` (+ `vara-cat.service`) |

PTT for VARA: VARA's CAT (Kenwood TS-2000) on Wine COM10 → `socat` virtual COM pair →
Hamlib `rigctlcom` → rigctld. rigctld stays the only program on the Digirig serial port,
so RigBroker sees every transmission (stuck-TX watchdog). The firewall opens 8010 and
8300/8301 **only to the Winlink PC** (`WINLINK_PC` in `deploy/nftables.conf`).
Install: `sudo sh deploy/install-remote-winlink.sh`.

## Winlink gateway planner (favorites)

A profile with `winlink_mode` (e.g. `winlink_pkt`) doesn't use a fixed gateway. A
**Winlink check** asks the planner for a ranked list and tries up to `max_attempts` gateways
**inside one lease**. If a gateway doesn't answer, or its channel stays busy, the broker
retunes to the next one, running the full safety sequence each time (TX idle, read-back
verify, clear channel). Pat only ever gets `pat connect ax25:///CALL` and never tunes the rig.

Ranking (hover a row in the UI to see the breakdown):

| Factor | Points |
|---|---|
| ★ favorite (config `favorite: true`, or the star in the UI) | +100 |
| your measured success rate (Laplace-smoothed) | up to +60 |
| last two attempts failed | −30 |
| distance from `winlink.grid` | −1 per 10 km (max −50) |
| HF band preferred for day/night at your grid | +20 |

* **Known-good spots:** put them in `winlink.gateways` with `favorite: true`, or add them in
  the UI (**+ Add gateway**). Stars, blocks and added gateways are saved in
  `winlink.state_file`. In mock mode the file gets a `.mock` suffix, so fake attempts never
  touch real history.
* **Safety:** a gateway is only usable if its frequency is inside the profile's
  `freq_ranges` (e.g. 144–148 MHz). Callsigns are strictly validated (`A-Z 0-9 -SSID`)
  because they end up in a command line.
* **Try one:** the **Try** button, `rigbrokerctl winlink vu --gateway W4ABC-10`, or
  `POST /radios/vu/winlink/check {"gateway": "W4ABC-10"}`.
* Gateway API: `GET /winlink/gateways?radio=vu`, `POST /winlink/gateways`,
  `DELETE /winlink/gateways/{call}`, `POST /winlink/gateways/{call}/favorite|block {"value": bool}`.
* Coming in milestone 5: candidates pulled automatically from `pat rmslist`, and VOACAP
  reliability for HF.

## CLI

```bash
.venv/bin/rigbrokerctl status
.venv/bin/rigbrokerctl lease vu monitor --minutes 30
.venv/bin/rigbrokerctl release vu
.venv/bin/rigbrokerctl winlink vu [--gateway W4ABC-10]
.venv/bin/rigbrokerctl gateways                 # ranked list, * = favorite, x = blocked
.venv/bin/rigbrokerctl fav W4ABC-10             # also: unfav, block, unblock
.venv/bin/rigbrokerctl clear-fault vu
.venv/bin/rigbrokerctl events --limit 20
```
`--url` (or `RIGBROKER_URL`) points it at another host; `--json` prints raw JSON.

## Safety model (summary)

* Only the broker changes freq/mode/power/squelch or starts/stops consumers.
* Before any change: rig must report RX for `tx_holdoff_s`; the consumer is stopped and RX
  re-confirmed. Every rig change is verified by read-back (freq, mode, RFPOWER); mismatch = FAULT.
* Transmitting profiles wait for a clear channel (DCD) before their consumer starts.
* Every lease has a max duration (profile `max_minutes`) and returns to the default profile.
* FAULT = TX inhibit: all TX-capable consumers stopped, alert raised, bounded automatic
  recovery (restart rigctld, reconnect, verify RX; optional USB power-cycle hook). Too many
  faults inside `fault_window_s` latches the fault until a manual clear.
* Stuck-TX watchdog (`stuck_tx_s`), frequency/mode drift detection on TX profiles.
* Without `--allow-tx`, TX-capable consumers and run jobs are never started.

## State machine

`BOOT → SWITCHING → DEFAULT ⇄ (SWITCHING → ACTIVE → RETURNING)`, any live state → `FAULT`.
The table lives in `rigbroker/state.py` (`table_rows()` prints it).

## Config gotcha

YAML reads unquoted `off`, `on`, `yes`, `no` as booleans. Quote profile names like `"off"`.
