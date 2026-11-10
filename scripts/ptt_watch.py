"""Watch PTT for a while: rigctld's PTT state and RigBroker's TX telemetry, logging changes.

Read-only (asks rigctld "t"; never sets anything). Usage:
  python3 scripts/ptt_watch.py [seconds] > /tmp/ptt-watch.log
"""
import json
import socket
import sys
import time
import urllib.request

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 300


def rig_ptt(sock: socket.socket) -> str:
    sock.sendall(b"t\n")
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(64)
        if not chunk:
            raise ConnectionError("rigctld closed")
        data += chunk
    v = data.decode().strip()
    return "ON" if v == "1" else "off" if v == "0" else f"?{v}"


def broker_tx() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/radios/vu", timeout=2) as r:
            d = json.load(r)
        t = d["telemetry"]
        return f"{'TX' if t['ptt'] else 'rx'} ({d['state']}/{d['applied_profile']})"
    except Exception as exc:
        return f"? {exc}"


def main() -> None:
    sock = socket.create_connection(("127.0.0.1", 4542), timeout=5)
    t0 = time.monotonic()
    last = None
    on_since = None
    print(f"{time.strftime('%H:%M:%S')} watching PTT for {DURATION:.0f}s", flush=True)
    while time.monotonic() - t0 < DURATION:
        ptt = rig_ptt(sock)
        if ptt != last:
            now = time.monotonic()
            extra = ""
            if ptt == "ON":
                on_since = now
            elif last == "ON" and on_since is not None:
                extra = f"  (was ON for {now - on_since:.1f}s)"
            print(f"{time.strftime('%H:%M:%S')} rigctld PTT {ptt}{extra} | broker: {broker_tx()}",
                  flush=True)
            last = ptt
        elif ptt == "ON" and int((time.monotonic() - t0) * 5) % 5 == 0:
            print(f"{time.strftime('%H:%M:%S')}   still ON | broker: {broker_tx()}", flush=True)
        time.sleep(0.2)
    print(f"{time.strftime('%H:%M:%S')} done", flush=True)


if __name__ == "__main__":
    main()
