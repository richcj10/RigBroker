"""KISS framing + AX.25 UI frames <-> TNC2 text, and a reconnecting KISS-over-TCP link.

TNC2 text is the familiar ``SRC>DEST,DIGI1*,WIDE2-1:info`` form (``*`` = last digipeater
that has repeated the frame). Info bytes are mapped 1:1 via latin-1 so Mic-E survives.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Protocol

log = logging.getLogger("rigbroker.kiss")

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
_ADDR_RE = re.compile(r"^([A-Z0-9]{1,6})(?:-(\d{1,2}))?(\*?)$")


class FrameError(ValueError):
    """Not a valid AX.25 UI frame / TNC2 string."""


class LinkDown(Exception):
    """The KISS link isn't connected."""


# ---------------------------------------------------------------- KISS


def kiss_escape(data: bytes) -> bytes:
    return data.replace(bytes([FESC]), bytes([FESC, TFESC])).replace(
        bytes([FEND]), bytes([FESC, TFEND]))


def kiss_frame(payload: bytes, port: int = 0) -> bytes:
    """Wrap an AX.25 frame as a KISS data frame."""
    return bytes([FEND, (port & 0x0F) << 4]) + kiss_escape(payload) + bytes([FEND])


class KissDecoder:
    """Incremental KISS de-framer: feed bytes, get complete data frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        for b in data:
            if b == FEND:
                if self._buf:
                    frame = self._unescape(bytes(self._buf))
                    if frame and frame[0] & 0x0F == 0:  # data frame (command 0)
                        out.append(frame[1:])
                    self._buf.clear()
            else:
                self._buf.append(b)
        return out

    @staticmethod
    def _unescape(data: bytes) -> bytes:
        out = bytearray()
        esc = False
        for b in data:
            if esc:
                out.append(FEND if b == TFEND else FESC if b == TFESC else b)
                esc = False
            elif b == FESC:
                esc = True
            else:
                out.append(b)
        return bytes(out)


# ---------------------------------------------------------------- AX.25


def _enc_addr(addr: str, *, last: bool, h_bit: bool = False) -> bytes:
    m = _ADDR_RE.match(addr.upper())
    if not m:
        raise FrameError(f"bad AX.25 address {addr!r}")
    call, ssid = m.group(1), int(m.group(2) or 0)
    if ssid > 15:
        raise FrameError(f"SSID > 15 in {addr!r}")
    body = bytes((ord(c) << 1) for c in call.ljust(6))
    last_b = 0x60 | (ssid << 1) | (0x80 if h_bit else 0) | (0x01 if last else 0)
    return body + bytes([last_b])


def _dec_addr(b: bytes) -> tuple[str, bool, bool]:
    call = "".join(chr(x >> 1) for x in b[:6]).strip()
    ssid = (b[6] >> 1) & 0x0F
    return (f"{call}-{ssid}" if ssid else call), bool(b[6] & 0x80), bool(b[6] & 0x01)


def tnc2_to_ax25(tnc2: str) -> bytes:
    """``SRC>DEST,PATH:info`` -> AX.25 UI frame bytes."""
    head, sep, info = tnc2.partition(":")
    if not sep or ">" not in head:
        raise FrameError(f"not TNC2: {tnc2!r}")
    src, _, rest = head.partition(">")
    dest, *digis = rest.split(",")
    if len(digis) > 8:
        raise FrameError("more than 8 digipeaters")
    used = max((i for i, d in enumerate(digis) if d.endswith("*")), default=-1)
    out = _enc_addr(dest, last=False) + _enc_addr(src, last=not digis)
    for i, d in enumerate(digis):
        out += _enc_addr(d.rstrip("*"), last=i == len(digis) - 1, h_bit=i <= used)
    return out + bytes([0x03, 0xF0]) + info.encode("latin-1")


def ax25_to_tnc2(frame: bytes) -> str:
    """AX.25 UI frame bytes -> TNC2 text. Raises FrameError for non-UI frames."""
    addrs: list[tuple[str, bool]] = []
    i = 0
    while True:
        if i + 7 > len(frame):
            raise FrameError("truncated address field")
        call, h, last = _dec_addr(frame[i:i + 7])
        addrs.append((call, h))
        i += 7
        if last:
            break
        if len(addrs) > 10:
            raise FrameError("too many addresses")
    if len(addrs) < 2 or i + 2 > len(frame) or frame[i] != 0x03 or frame[i + 1] != 0xF0:
        raise FrameError("not an AX.25 UI frame with PID F0")
    dest, src, digis = addrs[0][0], addrs[1][0], addrs[2:]
    used = max((n for n, (_, h) in enumerate(digis) if h), default=-1)
    path = [d + ("*" if n == used else "") for n, (d, _) in enumerate(digis)]
    return f"{src}>{','.join([dest, *path])}:{frame[i + 2:].decode('latin-1')}"


# ---------------------------------------------------------------- links

FrameHandler = Callable[[str], Awaitable[None]]


class AprsLink(Protocol):
    """Source/sink of APRS frames as TNC2 text."""

    connected: bool

    async def run(self, on_frame: FrameHandler) -> None: ...
    async def send(self, tnc2: str) -> None: ...


class KissTcpLink:
    """KISS over TCP to Direwolf (KISSPORT). Reconnects forever with backoff."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.connected = False
        self._writer: asyncio.StreamWriter | None = None

    async def run(self, on_frame: FrameHandler) -> None:
        delay = 1.0
        while True:
            try:
                reader, self._writer = await asyncio.open_connection(self.host, self.port)
            except OSError:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            self.connected, delay = True, 1.0
            log.info("KISS connected to %s:%d", self.host, self.port)
            dec = KissDecoder()
            try:
                while data := await reader.read(4096):
                    for frame in dec.feed(data):
                        try:
                            await on_frame(ax25_to_tnc2(frame))
                        except FrameError as exc:
                            log.debug("dropped frame: %s", exc)
            except OSError as exc:
                log.warning("KISS link error: %s", exc)
            finally:
                self.connected = False
                w, self._writer = self._writer, None
                if w is not None:
                    w.close()
                    with contextlib.suppress(OSError):
                        await w.wait_closed()
            await asyncio.sleep(delay)

    async def send(self, tnc2: str) -> None:
        w = self._writer
        if w is None or not self.connected:
            raise LinkDown(f"not connected to Direwolf KISS {self.host}:{self.port}")
        w.write(kiss_frame(tnc2_to_ax25(tnc2)))
        await w.drain()
