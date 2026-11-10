"""Icom GPS position parsing (made-up coordinates: Statue of Liberty)."""

from __future__ import annotations

import pytest

from rigbroker.gps import REPLY_LEN, parse_position, request, to_grid
from rigbroker.mock_rig import MockRig
from rigbroker.rig_client import RigError

CIV = 0x9A
# 40 41.2500' N, 074 02.7000' W, 93.5 m, 2026-01-02 03:04:05Z
ANSWER = bytes([0xFE, 0xFE, 0xE0, CIV, 0x23, 0x00,
                0x40, 0x41, 0x25, 0x00, 0x01,
                0x00, 0x74, 0x02, 0x70, 0x00, 0x00,
                0x00, 0x00, 0x93, 0x50,
                0x00, 0x14, 0x00, 0x00, 0x00,
                0x20, 0x26, 0x01, 0x02, 0x03, 0x04, 0x05, 0xFD])


def test_request_bytes() -> None:
    assert request(CIV) == bytes([0xFE, 0xFE, 0x9A, 0xE0, 0x23, 0x00, 0xFD])
    assert len(request(CIV) + ANSWER) == REPLY_LEN


def test_parse_with_echo() -> None:
    p = parse_position(request(CIV) + ANSWER, CIV, now=1.0)
    assert p is not None
    assert p.lat == pytest.approx(40 + 41.25 / 60, abs=1e-6)
    assert p.lon == pytest.approx(-(74 + 2.7 / 60), abs=1e-6)
    assert p.alt_m == pytest.approx(93.5)
    assert p.utc == "2026-01-02T03:04:05Z"
    assert to_grid(p.lat, p.lon) == "FN20xq"


def test_south_east_and_negative_altitude() -> None:
    a = bytearray(ANSWER)
    a[10] = 0x00  # S
    a[16] = 0x01  # E
    a[20] = 0x51  # sign nibble -> negative
    p = parse_position(bytes(a), CIV)
    assert p is not None and p.lat < 0 and p.lon > 0 and p.alt_m == pytest.approx(-93.5)


@pytest.mark.parametrize("raw", [
    b"",
    request(CIV),                                      # echo only
    bytes([0xFE, 0xFE, 0xE0, CIV, 0xFA, 0xFD]),        # NG (no fix / not supported)
    bytes([0xFE, 0xFE, 0xE0, CIV, 0x23, 0x00]) + bytes(27) + b"\xfd",  # zeros = no fix
    bytes([0xFE, 0xFE, 0xE0, CIV, 0x23, 0x00, 0xAB, 0xCD, 0x00, 0x00, 0x01,
           0, 0, 0, 0, 0, 0, 0xFD]),                    # not BCD
])
def test_no_fix_or_garbage(raw: bytes) -> None:
    assert parse_position(raw, CIV) is None


def test_grid() -> None:
    assert to_grid(0.5, 0.5, 4) == "JJ00"


async def test_mock_send_raw() -> None:
    rig = MockRig()
    with pytest.raises(RigError):
        await rig.send_raw(request(CIV), REPLY_LEN)
    rig.raw_reply = ANSWER
    assert await rig.send_raw(request(CIV), REPLY_LEN) == request(CIV) + ANSWER


def test_missing_altitude_is_still_a_fix() -> None:
    a = bytearray(ANSWER)
    a[17:21] = b"\xff\xff\xff\xff"  # radio: altitude not available
    p = parse_position(bytes(a), CIV)
    assert p is not None and p.alt_m is None and p.utc == "2026-01-02T03:04:05Z"
