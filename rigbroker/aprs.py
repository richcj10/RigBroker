"""APRS station: heard stations, two-way messaging (ack/retry), packet log.

Frames come from Direwolf over KISS (or a mock link). The broker decides when this service
may *hear* (an APRS consumer is running on the radio) and *transmit* (the TX-capable APRS
consumer is running, which also implies ``--allow-tx``); see ``Broker.aprs_gate``.
Nothing here transmits on its own except acks for messages addressed to ``callsign``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import math
import random
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import aprslib

from .config import AprsConfig, normalize_call
from .events import EventLog, Level
from .kiss import AprsLink, LinkDown

MAX_TEXT = 67
_BAD_TEXT = set("|~{")


class AprsError(Exception):
    """Refused APRS request (user-readable)."""


@dataclass
class Station:
    call: str
    lat: float | None = None
    lon: float | None = None
    symbol: str = "//"
    comment: str = ""
    status: str = ""
    last_heard: float = 0.0
    packets: int = 0
    via: str = "direct"
    speed_kmh: float | None = None
    course: int | None = None
    altitude_m: float | None = None
    weather: dict[str, Any] | None = None
    distance_km: float | None = None
    bearing: float | None = None


@dataclass
class Message:
    direction: str  # in | out
    peer: str  # the other station
    to: str  # addressee as sent
    text: str
    msgno: str | None
    state: str  # received | sending | retrying | acked | rejected | failed | cancelled | sent
    mine: bool = True  # addressed to this station's callsign (False: one of other_calls)
    ts: float = field(default_factory=time.time)
    tries: int = 0
    next_try: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])


@dataclass
class PacketRec:
    ts: float
    dir: str  # rx | tx
    raw: str
    fmt: str
    src: str
    summary: str


def bearing_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """(km, initial bearing in degrees) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return km, (math.degrees(math.atan2(y, x)) + 360) % 360


def validate_text(text: str) -> str:
    t = text.strip()
    if not t:
        raise AprsError("message is empty")
    if len(t) > MAX_TEXT:
        raise AprsError(f"message is {len(t)} characters; APRS allows {MAX_TEXT}")
    if any(not 32 <= ord(c) <= 126 or c in _BAD_TEXT for c in t):
        raise AprsError("message: printable ASCII only, no | ~ {")
    return t


Gate = Callable[[], tuple[bool, str]]
Position = Callable[[], tuple[float, float] | None]


class AprsService:
    """Owns the APRS link, station table, messages and packet log."""

    def __init__(self, cfg: AprsConfig, link: AprsLink, events: EventLog, *,
                 rx_gate: Gate, tx_gate: Gate, position: Position) -> None:
        self.cfg = cfg
        self.link = link
        self.events = events
        self.rx_gate = rx_gate
        self.tx_gate = tx_gate
        self.position = position
        self.callsign = cfg.callsign
        self.stations: dict[str, Station] = {}
        self.messages: deque[Message] = deque(maxlen=500)
        self.packets: deque[PacketRec] = deque(maxlen=cfg.max_packets)
        self.bulletins: dict[str, dict[str, Any]] = {}
        self._seen: dict[tuple[str, str], float] = {}  # (src, msgno) -> ts, for dedupe
        self._msgno = itertools.count(random.randint(1, 900))
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self.dropped_while_off = 0

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self.link.run(self.on_frame), name="aprs-link"),
                       asyncio.create_task(self._retry_loop(), name="aprs-retry")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    def _push(self, kind: str, data: dict[str, Any]) -> None:
        for q in list(self._subs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait({"kind": kind, "data": data})

    # ---------------------------------------------------------------- receive

    async def on_frame(self, raw: str) -> None:
        """Handle one received frame (TNC2 text)."""
        ok, _ = self.rx_gate()
        if not ok:  # Direwolf isn't ours to listen to right now (e.g. Winlink modem)
            self.dropped_while_off += 1
            return
        try:
            pkt = aprslib.parse(raw)
        except (aprslib.ParseError, aprslib.UnknownFormat) as exc:
            src = raw.split(">", 1)[0][:10]
            self._log_packet("rx", raw, "unparsed", src, str(exc)[:80])
            return
        except Exception as exc:  # aprslib can raise odd errors on garbage
            self._log_packet("rx", raw, "unparsed", "?", f"{type(exc).__name__}")
            return
        src = str(pkt.get("from", "?"))
        fmt = str(pkt.get("format", "?"))
        if fmt == "message" and "addresse" in pkt:
            await self._on_message(pkt, raw)
            return
        if fmt == "bulletin":
            key = f"{src}:{pkt.get('bid', '')}"
            self.bulletins[key] = {"from": src, "bid": pkt.get("bid"),
                                   "text": pkt.get("message_text", ""), "ts": time.time()}
            self._log_packet("rx", raw, fmt, src, f"BLN{pkt.get('bid', '')}: "
                             f"{pkt.get('message_text', '')}")
            self._push("bulletin", self.bulletins[key])
            return
        st = self._update_station(src, pkt)
        self._log_packet("rx", raw, fmt, src, self._summary(pkt, st))

    def _update_station(self, src: str, p: dict[str, Any]) -> Station:
        st = self.stations.get(src) or Station(call=src)
        st.last_heard, st.packets = time.time(), st.packets + 1
        path = [str(x) for x in p.get("path", [])]
        used = [x.rstrip("*") for x in path if x.endswith("*")]
        st.via = used[-1] if used else "direct"
        if "latitude" in p and "longitude" in p:
            st.lat, st.lon = float(p["latitude"]), float(p["longitude"])
            st.symbol = f"{p.get('symbol_table', '/')}{p.get('symbol', '/')}"
            if p.get("comment"):
                st.comment = str(p["comment"])[:80]
            st.speed_kmh = p.get("speed")
            st.course = p.get("course")
            st.altitude_m = p.get("altitude")
        if p.get("status"):
            st.status = str(p["status"])[:80]
        if p.get("weather"):
            st.weather = dict(p["weather"])
        me = self.position()
        if me and st.lat is not None and st.lon is not None:
            st.distance_km, st.bearing = bearing_distance(me[0], me[1], st.lat, st.lon)
        self.stations[src] = st
        self._push("station", asdict(st))
        return st

    @staticmethod
    def _summary(p: dict[str, Any], st: Station) -> str:
        fmt = p.get("format")
        if fmt == "status":
            return f"status: {p.get('status', '')}"
        if st.weather and p.get("weather"):
            w = p["weather"]
            parts = []
            if w.get("temperature") is not None:
                parts.append(f"{w['temperature']:.1f} C")
            if w.get("humidity") is not None:
                parts.append(f"{w['humidity']}% RH")
            wind = w.get("wind_speed", p.get("speed"))  # WX "course/speed" = wind dir/speed
            if wind is not None:
                parts.append(f"wind {float(wind):.0f} km/h from {p.get('course', '?')}")
            if w.get("pressure") is not None:
                parts.append(f"{w['pressure']:.0f} hPa")
            return "weather: " + ", ".join(parts)
        if "latitude" in p:
            extra = f" {st.distance_km:.1f} km" if st.distance_km is not None else ""
            return f"position {p['latitude']:.4f},{p['longitude']:.4f}{extra} {st.comment}"
        return str(fmt)

    async def _on_message(self, p: dict[str, Any], raw: str) -> None:
        src = str(p["from"])
        to = str(p["addresse"]).strip().upper()
        msgno = p.get("msgNo")
        resp = p.get("response")
        mine = to == self.callsign
        ours = mine or to in self.cfg.other_calls
        if resp in ("ack", "rej"):
            self._log_packet("rx", raw, resp, src, f"{resp}{msgno} to {to}")
            if mine and msgno:
                for m in self.messages:
                    if (m.direction == "out" and m.peer == src and m.msgno == msgno
                            and m.state in ("sending", "retrying")):
                        m.state = "acked" if resp == "ack" else "rejected"
                        verb = "acknowledged" if resp == "ack" else "REJECTED"
                        self._msg_event(m, f"{src} {verb} message {msgno}")
            return
        text = str(p.get("message_text", ""))
        self._log_packet("rx", raw, "message", src, f"to {to}: {text}")
        if not ours:
            return  # someone else's conversation; it stays in the packet log
        key = (src, str(msgno))
        dup = msgno is not None and key in self._seen
        if msgno is not None:
            self._seen[key] = time.time()
        if not dup:
            m = Message("in", src, to, text, msgno, "received", mine=mine)
            self.messages.append(m)
            self._msg_event(m, f"message from {src}" + ("" if mine else f" to {to}") +
                            f": {text}")
        if mine and msgno:  # APRS: ack every copy, including duplicates
            try:
                await self._transmit(f"{self._head()}::{src:<9}:ack{msgno}", "ack", src,
                                     f"ack{msgno} to {src}")
            except AprsError as exc:
                self.events.emit("aprs", f"could not ack {src} msg {msgno}: {exc}",
                                 level="warning")

    # ---------------------------------------------------------------- transmit

    def _head(self) -> str:
        return f"{self.callsign}>{','.join([self.cfg.tocall, *self.cfg.msg_path])}"

    async def _transmit(self, tnc2: str, fmt: str, peer: str, summary: str) -> None:
        ok, why = self.tx_gate()
        if not ok:
            raise AprsError(why)
        try:
            await self.link.send(tnc2)
        except LinkDown as exc:
            raise AprsError(str(exc)) from None
        self._log_packet("tx", tnc2, fmt, self.callsign, summary)

    async def send_message(self, to: str, text: str) -> Message:
        """Queue and send a message (retried until acked). Raises AprsError."""
        try:
            dest = normalize_call(to)
        except ValueError as exc:
            raise AprsError(str(exc)) from None
        body = validate_text(text)
        if dest == self.callsign:
            raise AprsError("that's this station's own callsign")
        ok, why = self.tx_gate()
        if not ok:
            raise AprsError(why)
        m = Message("out", dest, dest, body, str(next(self._msgno) % 100000), "sending")
        self.messages.append(m)
        await self._send_try(m)
        return m

    async def cancel(self, msg_id: str) -> Message:
        for m in self.messages:
            if m.id == msg_id and m.direction == "out":
                if m.state in ("sending", "retrying"):
                    m.state = "cancelled"
                    self._msg_event(m, f"message {m.msgno} to {m.peer} cancelled")
                return m
        raise AprsError(f"no outgoing message {msg_id}")

    async def _send_try(self, m: Message) -> None:
        m.tries += 1
        m.next_try = time.monotonic() + self.cfg.msg_retry_s * 2 ** (m.tries - 1)
        try:
            await self._transmit(f"{self._head()}::{m.to:<9}:{m.text}{{{m.msgno}", "message",
                                 m.peer, f"to {m.peer}: {m.text} (try {m.tries})")
        except AprsError as exc:
            self.events.emit("aprs", f"message {m.msgno} to {m.peer} not sent: {exc}",
                             level="warning")
        if m.tries > 1 and m.state == "sending":
            m.state = "retrying"
        self._msg_event(m, None)

    async def _retry_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            for m in list(self.messages):
                if m.direction != "out" or m.state not in ("sending", "retrying"):
                    continue
                if now < m.next_try:
                    continue
                if m.tries >= self.cfg.msg_retries:
                    m.state = "failed"
                    self._msg_event(m, f"message {m.msgno} to {m.peer}: no ack after "
                                       f"{m.tries} tries")
                else:
                    await self._send_try(m)
            self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self.cfg.station_timeout_min * 60
        for call in [c for c, s in self.stations.items() if s.last_heard < cutoff]:
            del self.stations[call]
            self._push("station_gone", {"call": call})
        old = time.time() - 3600
        for k in [k for k, ts in self._seen.items() if ts < old]:
            del self._seen[k]

    # ---------------------------------------------------------------- views

    def _log_packet(self, d: str, raw: str, fmt: str, src: str, summary: str) -> None:
        rec = PacketRec(time.time(), d, raw, fmt, src, summary)
        self.packets.append(rec)
        self._push("packet", asdict(rec))

    def _msg_event(self, m: Message, text: str | None) -> None:
        self._push("message", asdict(m))
        if text:
            level: Level = "warning" if m.state in ("failed", "rejected") else "info"
            self.events.emit("aprs_message", text, level=level, peer=m.peer, state=m.state)

    def status(self) -> dict[str, Any]:
        rx, rx_why = self.rx_gate()
        tx, tx_why = self.tx_gate()
        me = self.position()
        return {"callsign": self.callsign, "other_calls": self.cfg.other_calls,
                "link": self.link.connected, "rx": rx, "rx_reason": rx_why, "tx": tx,
                "tx_reason": tx_why, "position": list(me) if me else None,
                "stations": len(self.stations), "max_text": MAX_TEXT}

    def station_list(self) -> list[dict[str, Any]]:
        return [asdict(s) for s in self.stations.values()]

    def message_list(self) -> list[dict[str, Any]]:
        return [asdict(m) for m in self.messages]

    def snapshot(self) -> dict[str, Any]:
        return {"status": self.status(),
                "stations": self.station_list(),
                "messages": self.message_list(),
                "packets": [asdict(p) for p in list(self.packets)[-200:]],
                "bulletins": list(self.bulletins.values())}
