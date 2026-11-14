"""Packet BBS terminal: AX.25 *connected mode* through Direwolf's AGW port.

A session leases the radio on the ``bbs.profile`` profile (direwolf@pkt), tuned to the BBS's
frequency (a lease plan step without a ``run`` command = tune and hold), then asks Direwolf
over AGW to connect - directly or via digipeaters. Direwolf does the AX.25 link layer
(SABM/UA, I-frames, retries, acks); this module only moves text between the link and the
web UI. Every way a session can end (user, remote, lease expiry/preemption, Direwolf gone)
closes the AGW link and gives the radio back.

RF text is untrusted: it is sanitised here and the web UI only ever renders it as text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import struct
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .config import BbsConfig, BbsEntry, normalize_call
from .events import EventLog
from .leases import PlanStep
from .state import RadioState

if TYPE_CHECKING:
    from .radio import RadioController

log = logging.getLogger("rigbroker.bbs")

PID_NO_L3 = 0xF0
MAX_LINE = 250

# ---------------------------------------------------------------- AGW frames

_HDR = struct.Struct("<B3xcxBx10s10sII")  # port, kind, pid, from, to, data len, user
HDR_LEN = _HDR.size  # 36


class BbsError(Exception):
    """A refused BBS request (user-readable)."""


def _call10(call: str) -> bytes:
    return call.encode("ascii")[:9].ljust(10, b"\0")


def _uncall(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("ascii", "replace").strip().upper()


@dataclass
class AgwFrame:
    kind: str  # one character, e.g. "C", "D", "d", "v", "X"
    call_from: str = ""
    call_to: str = ""
    data: bytes = b""
    port: int = 0
    pid: int = 0

    def encode(self) -> bytes:
        return _HDR.pack(self.port, self.kind.encode("ascii"), self.pid,
                         _call10(self.call_from), _call10(self.call_to),
                         len(self.data), 0) + self.data


def via_data(digis: list[str]) -> bytes:
    """Data of a 'v' (connect via) frame: digipeater count, then 10-byte calls."""
    return bytes([len(digis)]) + b"".join(_call10(d) for d in digis)


class AgwDecoder:
    """Incremental AGW de-framer: feed bytes, get complete frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[AgwFrame]:
        self._buf += data
        out: list[AgwFrame] = []
        while len(self._buf) >= HDR_LEN:
            port, kind, pid, cf, ct, n, _user = _HDR.unpack_from(self._buf)
            if n > 65536:
                raise BbsError(f"AGW frame claims {n} data bytes")
            if len(self._buf) < HDR_LEN + n:
                break
            payload = bytes(self._buf[HDR_LEN:HDR_LEN + n])
            del self._buf[:HDR_LEN + n]
            out.append(AgwFrame(kind.decode("latin-1"), _uncall(cf), _uncall(ct), payload,
                                port, pid))
        return out


def clean_rx(data: bytes) -> str:
    """BBS bytes -> safe display text: CR/CRLF -> LF, no control chars except LF/TAB."""
    text = data.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text
                   if c in "\n\t" or (32 <= ord(c) < 127) or ord(c) >= 160)


def clean_tx(line: str) -> str:
    """A typed line -> what is sent (without the CR). Raises BbsError."""
    if len(line) > MAX_LINE:
        raise BbsError(f"line is {len(line)} characters; at most {MAX_LINE}")
    if any(not 32 <= ord(c) <= 126 for c in line):
        raise BbsError("printable ASCII only")
    return line


# ---------------------------------------------------------------- AGW connection


class AgwConn:
    """One TCP connection to an AGW server (Direwolf)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self._dec = AgwDecoder()
        self._pending: deque[AgwFrame] = deque()

    @classmethod
    async def open(cls, host: str, port: int, *, wait_s: float = 15.0) -> AgwConn:
        """Connect, retrying while Direwolf is still starting. Raises OSError."""
        end = time.monotonic() + wait_s
        while True:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
                return cls(r, w)
            except (OSError, TimeoutError):
                if time.monotonic() >= end:
                    raise
                await asyncio.sleep(0.5)

    async def send(self, frame: AgwFrame) -> None:
        self.writer.write(frame.encode())
        await self.writer.drain()

    async def recv(self) -> AgwFrame:
        """Next frame. Raises ConnectionError when the server closes."""
        while not self._pending:
            data = await self.reader.read(4096)
            if not data:
                raise ConnectionError("AGW connection closed")
            self._pending.extend(self._dec.feed(data))
        return self._pending.popleft()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 2)


# ---------------------------------------------------------------- station list


class BbsStore:
    """BBSes added in the web UI (JSON, atomic writes); config entries come first."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(os.path.expanduser(path)) if path else None
        self.user: dict[str, BbsEntry] = {}
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.user = {k: BbsEntry(**v) for k, v in data.get("stations", {}).items()}
            except (OSError, ValueError, TypeError) as exc:
                bad = self.path.with_suffix(f".corrupt-{int(time.time())}")
                log.error("BBS list %s unreadable (%s); moved to %s", self.path, exc, bad)
                self.path.replace(bad)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".bbs-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"stations": {k: v.model_dump() for k, v in self.user.items()}}, f,
                      indent=1)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- session service

State = Literal["idle", "tuning", "connecting", "connected", "disconnecting"]


@dataclass
class Session:
    call: str
    via: list[str]
    holder: str
    freq: int
    state: State = "tuning"
    lease_id: str | None = None
    since: float = field(default_factory=time.time)


class BbsService:
    """One BBS session at a time on one radio."""

    def __init__(self, cfg: BbsConfig, mycall: str, radio: RadioController,
                 events: EventLog, *, tx_allowed: Callable[[], bool],
                 agw_addr: Callable[[], tuple[str, int]] | None = None) -> None:
        self.cfg = cfg
        self.mycall = mycall
        self.radio = radio
        self.events = events
        self.tx_allowed = tx_allowed
        self.agw_addr = agw_addr or (lambda: (cfg.agw_host, cfg.agw_port))
        self.store = BbsStore(cfg.state_file)
        self.session: Session | None = None
        self.last: dict[str, Any] | None = None  # how the previous session ended
        self.scrollback: deque[dict[str, str]] = deque(maxlen=cfg.scrollback_lines)
        self._conn: AgwConn | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    # ------------------------------------------------------------ list

    def stations(self) -> list[BbsEntry]:
        out = {e.call: e for e in self.cfg.stations}
        out.update(self.store.user)
        return list(out.values())

    def station(self, call: str) -> BbsEntry:
        c = normalize_call(call)
        for e in self.stations():
            if e.call == c:
                return e
        raise BbsError(f"unknown BBS {c}")

    def add_station(self, entry: BbsEntry) -> BbsEntry:
        prof = self.radio.cfg.profiles[self.cfg.profile]
        if not prof.freq_allowed(entry.freq):
            raise BbsError(f"{entry.freq} Hz is outside the {self.cfg.profile} profile's "
                           "allowed ranges")
        self.store.user[entry.call] = entry
        self.store.save()
        return entry

    def remove_station(self, call: str) -> None:
        c = normalize_call(call)
        if c not in self.store.user:
            raise KeyError(f"{c} was not added in the web UI")
        del self.store.user[c]
        self.store.save()

    # ------------------------------------------------------------ subscribers

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

    def _text(self, direction: str, text: str) -> None:
        if not text:
            return
        chunk = {"dir": direction, "text": text, "ts": f"{time.time():.3f}"}
        self.scrollback.append(chunk)
        self._push("text", chunk)

    def _set_state(self, state: State) -> None:
        if self.session is not None:
            self.session.state = state
        self._push("status", self.status())

    def status(self) -> dict[str, Any]:
        s = self.session
        return {"mycall": self.mycall, "radio": self.radio.id, "profile": self.cfg.profile,
                "state": s.state if s else "idle",
                "session": None if s is None else asdict(s), "last": self.last}

    def snapshot(self) -> dict[str, Any]:
        return {**self.status(), "stations": [e.model_dump() for e in self.stations()],
                "scrollback": list(self.scrollback)}

    # ------------------------------------------------------------ control

    async def connect(self, call: str, via: list[str], holder: str) -> Session:
        """Start a session (returns once the radio is leased). Raises BbsError."""
        async with self._lock:
            if self.session is not None:
                raise BbsError(f"a BBS session is already {self.session.state} "
                               f"({self.session.call} for {self.session.holder})")
            if not self.tx_allowed():
                raise BbsError("a BBS session transmits; the broker is read-only "
                               "(--allow-tx not set)")
            entry = self.station(call)
            digis = [normalize_call(d) for d in via]
            if len(digis) > 7:
                raise BbsError("at most 7 digipeaters")
            step = PlanStep(label=entry.call, freq=entry.freq, run="", key=entry.call,
                            timeout_s=self.cfg.connect_timeout_s)
            res = await self.radio.request_lease(self.cfg.profile, holder=holder, plan=[step])
            if not res.ok or res.lease is None:
                raise BbsError(f"radio not available: {res.reason}")
            path = f" via {','.join(digis)}" if digis else " direct"
            self.session = Session(entry.call, digis, holder, entry.freq, lease_id=res.lease.id)
            self._text("sys", f"*** Tuning to {entry.freq / 1e6:.4f} MHz for {entry.call}"
                              f"{path}...\n")
            self.events.emit("bbs", f"{holder} connecting to {entry.call}{path}",
                             radio=self.radio.id)
            self._set_state("tuning")
            self._task = asyncio.create_task(self._run(self.session), name="bbs-session")
            return self.session

    async def send(self, line: str) -> None:
        """Send one typed line (plus CR). Raises BbsError."""
        text = clean_tx(line)
        s, conn = self.session, self._conn
        if s is None or s.state != "connected" or conn is None:
            raise BbsError("not connected to a BBS")
        await conn.send(AgwFrame("D", self.mycall, s.call, (text + "\r").encode("ascii"),
                                 pid=PID_NO_L3))
        self._text("tx", text + "\n")

    async def disconnect(self, by: str) -> bool:
        """End the session (polite AX.25 disconnect if connected). False if none."""
        s = self.session
        if s is None:
            return False
        if s.state == "connected" and self._conn is not None:
            self._set_state("disconnecting")
            with contextlib.suppress(Exception):
                await self._conn.send(AgwFrame("d", self.mycall, s.call))
            # the session task ends when Direwolf confirms ('d'); don't wait forever
            if self._task is not None:
                done, _ = await asyncio.wait({self._task}, timeout=8)
                if done:
                    return True
        await self._finish(f"disconnected by {by}")
        return True

    async def stop(self) -> None:
        if self.session is not None:
            await self._finish("broker shutting down")

    # ------------------------------------------------------------ session task

    def _lease_alive(self, s: Session) -> bool:
        lease = self.radio.lease
        return lease is not None and lease.id == s.lease_id

    async def _run(self, s: Session) -> None:
        reason = "session ended"
        try:
            reason = await self._session(s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # every failure must give the radio back
            log.exception("BBS session failed")
            reason = f"error: {exc}"
        if self.session is s:
            await self._finish(reason, from_task=True)

    async def _session(self, s: Session) -> str:
        # 1. wait until the radio is tuned and the packet modem runs
        end = time.monotonic() + 90
        while not (self._lease_alive(s) and self.radio.state is RadioState.ACTIVE
                   and self.radio.consumer == "direwolf@pkt"):
            if not self._lease_alive(s):
                return "the radio was not available (lease ended)"
            if time.monotonic() > end:
                return "timed out waiting for the packet modem"
            await asyncio.sleep(0.3)
        # 2. AGW: register our call, connect (via digipeaters)
        host, port = self.agw_addr()
        try:
            conn = await AgwConn.open(host, port)
        except OSError as exc:
            return f"Direwolf AGW port {host}:{port} not reachable ({exc})"
        self._conn = conn
        await conn.send(AgwFrame("X", self.mycall))
        if s.via:
            await conn.send(AgwFrame("v", self.mycall, s.call, via_data(s.via), pid=PID_NO_L3))
        else:
            await conn.send(AgwFrame("C", self.mycall, s.call, pid=PID_NO_L3))
        self._set_state("connecting")
        path = f" via {','.join(s.via)}" if s.via else ""
        self._text("sys", f"*** Connecting to {s.call}{path} as {self.mycall}...\n")
        connect_by = time.monotonic() + self.cfg.connect_timeout_s
        # 3. relay until the link or the lease ends
        while True:
            if not self._lease_alive(s):
                return ("the radio session ended (Release pressed, time limit, "
                        "or taken by another user)")
            timeout = 1.0
            if s.state == "connecting":
                left = connect_by - time.monotonic()
                if left <= 0:
                    return f"{s.call} did not answer"
                timeout = min(timeout, left)
            try:
                fr = await asyncio.wait_for(conn.recv(), timeout)
            except TimeoutError:
                continue
            except ConnectionError:
                return "Direwolf closed the AGW connection"
            if fr.kind == "X":
                if fr.data[:1] != b"\x01":
                    self._text("sys", f"*** note: Direwolf did not register {self.mycall} "
                                      "(already in use?)\n")
                continue
            if fr.call_from and fr.call_from != s.call:
                continue  # some other station's traffic
            if fr.kind == "C":
                self._set_state("connected")
                self._text("sys", clean_rx(fr.data) or f"*** CONNECTED to {s.call}\n")
                self.events.emit("bbs", f"connected to {s.call}{path}", radio=self.radio.id)
            elif fr.kind == "D":
                self._text("rx", clean_rx(fr.data))
            elif fr.kind == "d":
                self._text("sys", clean_rx(fr.data) or f"*** DISCONNECTED from {s.call}\n")
                return "disconnected" if s.state != "connecting" else f"{s.call} did not answer"

    async def _finish(self, reason: str, *, from_task: bool = False) -> None:
        s = self.session
        if s is None:
            return
        self.session = None
        task, self._task = self._task, None
        if task is not None and not from_task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()
        if self.radio.lease is not None and self.radio.lease.id == s.lease_id:
            await self.radio.release(by=f"BBS session ({reason})")
        self.last = {"call": s.call, "reason": reason, "ended": time.time()}
        self._text("sys", f"*** Session ended: {reason}. Radio returned.\n")
        self.events.emit("bbs", f"session with {s.call} ended: {reason}", radio=self.radio.id)
        self._push("status", self.status())


# ---------------------------------------------------------------- mock BBS (UI work, tests)

BANNER = ("[KT7RUN-7 MOCK BBS]\rHello {you}, welcome.\r"
          "Commands: L (list), R n (read), B (bye)\rKT7RUN-7> ")
MSGS = [("1", "ALL", "Net tonight 8pm on the repeater"),
        ("2", "KY4WL", "Welcome to the BBS"),
        ("3", "ALL", "Packet field day this weekend")]


class MockAgwServer:
    """A tiny AGW server that behaves like Direwolf + a friendly BBS (no RF)."""

    def __init__(self, *, connect_delay: float = 0.5, answer: bool = True) -> None:
        self.connect_delay = connect_delay
        self.answer = answer
        self.received: list[AgwFrame] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        dec = AgwDecoder()
        peer: str | None = None
        me: str = ""

        async def send(kind: str, text: str = "") -> None:
            w.write(AgwFrame(kind, peer or "", me, text.encode("latin-1"),
                             pid=PID_NO_L3).encode())
            await w.drain()

        try:
            while data := await r.read(4096):
                for fr in dec.feed(data):
                    self.received.append(fr)
                    if fr.kind == "X":
                        w.write(AgwFrame("X", fr.call_from, data=b"\x01").encode())
                    elif fr.kind in ("C", "v"):
                        peer, me = fr.call_to, fr.call_from
                        await asyncio.sleep(self.connect_delay)
                        if not self.answer:
                            await send("d", f"*** DISCONNECTED RETRYOUT With {peer}\r")
                            continue
                        await send("C", f"*** CONNECTED With Station {peer}\r")
                        await send("D", BANNER.format(you=me))
                    elif fr.kind == "D" and peer:
                        await self._command(fr.data.decode("ascii", "replace").strip(), send)
                    elif fr.kind == "d" and peer:
                        await send("d", f"*** DISCONNECTED From Station {peer}\r")
                        peer = None
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                w.close()

    @staticmethod
    async def _command(cmd: str, send: Callable[..., Awaitable[None]]) -> None:
        c = cmd.upper()
        if c == "L":
            rows = "".join(f"{n:>3}  {to:<6} {subj}\r" for n, to, subj in MSGS)
            await send("D", f"Msg# To     Subject\r{rows}KT7RUN-7> ")
        elif c.startswith("R "):
            m = next((x for x in MSGS if x[0] == c[2:].strip()), None)
            await send("D", (f"Msg {m[0]} to {m[1]}: {m[2]}\r" if m else "No such message\r")
                       + "KT7RUN-7> ")
        elif c in ("B", "BYE"):
            await send("D", "73!\r")
            await send("d", "*** DISCONNECTED From Station KT7RUN-7\r")
        else:
            await send("D", f"? {cmd}\rKT7RUN-7> ")


__all__ = ["AgwConn", "AgwDecoder", "AgwFrame", "BbsError", "BbsService", "BbsStore",
           "MockAgwServer", "Session", "clean_rx", "clean_tx", "via_data"]
