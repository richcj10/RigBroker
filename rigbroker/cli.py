"""``rigbrokerctl``: command-line client for the RigBroker REST API (stdlib only).

  rigbrokerctl status
  rigbrokerctl lease vu monitor --minutes 30
  rigbrokerctl release vu
  rigbrokerctl winlink vu [--gateway W4ABC-10]
  rigbrokerctl gateways [--radio vu]
  rigbrokerctl fav W4ABC-10 | unfav W4ABC-10 | block K4BAD-10 | unblock K4BAD-10
  rigbrokerctl clear-fault vu
  rigbrokerctl events --limit 30

Commands that change anything need an API token (create one on the Pi with
``rigbroker-admin token create cli``): --token, $RIGBROKER_TOKEN, or the file
~/.config/rigbroker/token (keep it chmod 600).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# (method, path, json body) -> (status code, decoded json)
Requester = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]


TOKEN_FILE = "~/.config/rigbroker/token"


def find_token(explicit: str | None) -> str | None:
    """--token, then $RIGBROKER_TOKEN, then the token file."""
    if explicit:
        return explicit
    if os.environ.get("RIGBROKER_TOKEN"):
        return os.environ["RIGBROKER_TOKEN"]
    try:
        with open(os.path.expanduser(TOKEN_FILE), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def http_requester(base: str, timeout: float = 10.0, token: str | None = None) -> Requester:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def req(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                   headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except ValueError:
                return exc.code, {"reason": exc.reason}
    return req


def _mhz(hz: int | None) -> str:
    return f"{hz / 1e6:.4f} MHz" if hz else "-"


def format_status(st: dict[str, Any]) -> str:
    lines = [f"RigBroker {st['version']}  mode={'MOCK' if st['mock'] else 'HARDWARE'}  "
             f"allow_tx={st['allow_tx']}"]
    for rid, r in st["radios"].items():
        t = r["telemetry"]
        lines.append(f"\n[{rid}] {r['name']}  state={r['state'].upper()}  "
                     f"profile={r['applied_profile'] or '-'}"
                     + (f" -> {r['target_profile']}" if r["target_profile"] != r["applied_profile"]
                        else ""))
        lines.append(f"  rig: {'online' if t['online'] else 'OFFLINE'}  {_mhz(t['freq'])} "
                     f"{t['mode'] or ''}  {'TX' if t['ptt'] else 'RX'}  "
                     f"{'BUSY' if t['dcd'] else 'clear'}  S={t['strength']}")
        lines.append(f"  consumer: {r['consumer'] or '-'}")
        if r["lease"]:
            ls = r["lease"]
            lines.append(f"  lease {ls['id']}: {ls['profile']} by {ls['holder']} "
                         f"({ls['priority']}), {ls['remaining_s']}s left")
        for q in r["queue"]:
            lines.append(f"  queued {q['id']}: {q['profile']} by {q['holder']} ({q['priority']})")
        if r["fault"]:
            f = r["fault"]
            lines.append(f"  FAULT ({f['kind']}{', LATCHED' if f['latched'] else ''}): "
                         f"{f['reason']}")
    for rid in st.get("disabled_radios", []):
        lines.append(f"\n[{rid}] not managed (no profiles)")
    return "\n".join(lines)


def format_gateways(data: dict[str, Any]) -> str:
    lines = [f"grid={data['grid'] or '(not set)'}  period={data['period'] or '?'}",
             f"{'':2}{'GATEWAY':<11} {'FREQ':>12} {'MODE':<8} {'DIST':>6} {'OK/TRY':>7} "
             f"{'SCORE':>6}  NOTE"]
    for g in data["gateways"]:
        st = g["stats"]
        # blocked wins: a blocked favorite is never tried
        flag = "x" if g["blocked"] else "*" if g["favorite"] else " " if g["usable"] else "-"
        dist = "-" if g["distance_km"] is None else f"{g['distance_km']}km"
        lines.append(f"{flag:<2}{g['call']:<11} {_mhz(g['freq']):>12} {g['mode']:<8} {dist:>6} "
                     f"{st['successes']:>3}/{st['attempts']:<3} {g['score']:>6}  {g['note']}")
    lines.append("(* favorite, x blocked, - not usable on this radio's winlink profile)")
    return "\n".join(lines)


def format_events(evs: list[dict[str, Any]]) -> str:
    out = []
    for e in evs:
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        out.append(f"{ts} {e['level']:<7} [{e['radio'] or '-'}] {e['kind']}: {e['message']}")
    return "\n".join(out)


def main(argv: list[str] | None = None, request: Requester | None = None) -> int:
    p = argparse.ArgumentParser(prog="rigbrokerctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("RIGBROKER_URL", "http://127.0.0.1:8080"))
    p.add_argument("--json", action="store_true", help="raw JSON output")
    p.add_argument("--token", help="API token (default: $RIGBROKER_TOKEN or token file)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    lp = sub.add_parser("lease")
    lp.add_argument("radio")
    lp.add_argument("profile")
    lp.add_argument("--minutes", type=float)
    lp.add_argument("--priority", default="manual", choices=["manual", "scheduled"])
    lp.add_argument("--holder", default="cli")
    lp.add_argument("--queue", action="store_true")
    for name in ("release", "clear-fault"):
        sub.add_parser(name).add_argument("radio")
    wp = sub.add_parser("winlink")
    wp.add_argument("radio")
    wp.add_argument("--gateway", help="try only this gateway")
    gp = sub.add_parser("gateways")
    gp.add_argument("--radio", default="vu")
    for name in ("fav", "unfav", "block", "unblock"):
        sub.add_parser(name).add_argument("call")
    ep = sub.add_parser("events")
    ep.add_argument("--limit", type=int, default=30)
    ep.add_argument("--radio")
    a = p.parse_args(argv)
    req = request or http_requester(a.url, token=find_token(a.token))

    try:
        if a.cmd == "status":
            code, body = req("GET", "/status", None)
        elif a.cmd == "lease":
            code, body = req("POST", f"/radios/{a.radio}/lease", {
                "profile": a.profile, "minutes": a.minutes, "priority": a.priority,
                "holder": a.holder, "queue": a.queue})
        elif a.cmd == "release":
            code, body = req("POST", f"/radios/{a.radio}/release", {"holder": "cli"})
        elif a.cmd == "winlink":
            code, body = req("POST", f"/radios/{a.radio}/winlink/check",
                             {"holder": "cli", "gateway": a.gateway})
        elif a.cmd == "gateways":
            code, body = req("GET", f"/winlink/gateways?radio={a.radio}", None)
        elif a.cmd in ("fav", "unfav", "block", "unblock"):
            kind = "favorite" if a.cmd in ("fav", "unfav") else "block"
            code, body = req("POST", f"/winlink/gateways/{a.call}/{kind}",
                             {"value": not a.cmd.startswith("un")})
        elif a.cmd == "clear-fault":
            code, body = req("POST", f"/radios/{a.radio}/fault/clear", None)
        else:
            q = f"?limit={a.limit}" + (f"&radio={a.radio}" if a.radio else "")
            code, body = req("GET", "/events" + q, None)
    except (urllib.error.URLError, OSError) as exc:
        print(f"cannot reach rigbroker at {a.url}: {exc}", file=sys.stderr)
        return 3

    if a.json:
        print(json.dumps(body, indent=2))
    elif code >= 400:
        detail = body.get("reason") or body.get("detail") if isinstance(body, dict) else body
        print(f"error {code}: {detail}", file=sys.stderr)
    elif a.cmd == "status":
        print(format_status(body))
    elif a.cmd == "events":
        print(format_events(body))
    elif a.cmd == "gateways":
        print(format_gateways(body))
    elif a.cmd in ("fav", "unfav", "block", "unblock"):
        print(f"{a.call.upper()}: {a.cmd} ok")
    else:
        print(body.get("verdict") or ("ok" if body.get("ok") else ""), "-", body.get("reason"))
    return 0 if code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
