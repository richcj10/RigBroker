"""GPS position from an Icom radio with a built-in receiver (ID-4100, ID-5100, ID-51, ...).

CI-V command 0x23 0x00 ("read my position") returns, all BCD:

    lat  5 bytes  dd mm mm mm h      40 41 25 00 01    -> 40 deg 41.2500' N  (h: 01=N, 00=S)
    lon  6 bytes  0d dd mm mm mm h   00 74 02 70 00 00 -> 74 deg 02.7000' W  (h: 01=E, 00=W)
    alt  4 bytes  0.1 m              00 00 93 50       -> 93.5 m (last nibble: sign)
    course 2, speed 3, UTC time 7 (yyyy mm dd hh mm ss)

The command is read-only. It goes through rigctld (send_cmd_rx), never to the tty.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

CMD_READ_POSITION = (0x23, 0x00)
CONTROLLER = 0xE0
# echo of our 7-byte command + reply header 6 + 5+6+4+2+3+7 data + FD
REPLY_LEN = 7 + 6 + 27 + 1


@dataclass(frozen=True)
class Position:
    lat: float
    lon: float
    alt_m: float | None
    utc: str | None  # "2026-11-10T10:12:58Z" from the GPS
    read_at: float  # wall clock when we read it

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bcd(b: int) -> int:
    hi, lo = b >> 4, b & 0x0F
    if hi > 9 or lo > 9:
        raise ValueError(f"not BCD: {b:#04x}")
    return hi * 10 + lo


def _digits(bs: bytes) -> str:
    return "".join(f"{_bcd(b):02d}" for b in bs)


def request(civ_address: int) -> bytes:
    return bytes([0xFE, 0xFE, civ_address, CONTROLLER, *CMD_READ_POSITION, 0xFD])


def parse_position(raw: bytes, civ_address: int, *, now: float | None = None) -> Position | None:
    """Find the radio's 0x23 0x00 reply in ``raw`` (may include our echoed command).

    Returns None when the radio has no fix (it answers NG / 0xFA, or zeros)."""
    header = bytes([0xFE, 0xFE, CONTROLLER, civ_address, *CMD_READ_POSITION])
    i = raw.find(header)
    if i < 0:
        return None
    d = raw[i + len(header):]
    end = d.find(0xFD)
    if end >= 0:
        d = d[:end]
    if len(d) < 11:
        return None
    try:
        la = _digits(d[0:4])                       # ddmmmmmm
        lat = int(la[0:2]) + int(la[2:]) / 10000 / 60
        if d[4] == 0x00:
            lat = -lat
        lo = _digits(d[5:10])                      # 0dddmmmmmm
        lon = int(lo[0:4]) + int(lo[4:]) / 10000 / 60
        if d[10] == 0x00:
            lon = -lon
    except ValueError:
        return None
    # optional fields: the radio sends FF FF.. when it has no value (e.g. no 3D fix)
    alt: float | None = None
    if len(d) >= 15:
        try:
            a = _digits(d[11:15])                  # 0.1 m, 8 digits; last nibble = sign
            alt = int(a[:7]) / 10 * (-1 if a[7] == "1" else 1)
        except ValueError:
            alt = None
    utc: str | None = None
    if len(d) >= 27:
        try:
            t = _digits(d[20:27])                  # yyyy mm dd hh mm ss
            utc = f"{t[0:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}Z"
        except ValueError:
            utc = None
    if lat == 0 and lon == 0:
        return None  # no fix
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return Position(round(lat, 6), round(lon, 6), alt, utc,
                    time.time() if now is None else now)


def to_grid(lat: float, lon: float, chars: int = 6) -> str:
    """Maidenhead locator (4 or 6 characters)."""
    lon += 180
    lat += 90
    g = chr(ord("A") + int(lon / 20)) + chr(ord("A") + int(lat / 10))
    g += str(int(lon % 20 / 2)) + str(int(lat % 10))
    if chars >= 6:
        g += chr(ord("a") + int(lon % 2 * 12)) + chr(ord("a") + int(lat % 1 * 24))
    return g
