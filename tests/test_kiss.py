from __future__ import annotations

import asyncio

import pytest

from rigbroker.kiss import (
    FEND,
    FESC,
    FrameError,
    KissDecoder,
    KissTcpLink,
    LinkDown,
    ax25_to_tnc2,
    kiss_frame,
    tnc2_to_ax25,
)


@pytest.mark.parametrize("tnc2", [
    "KY4WL-5>APZRBK,WIDE1-1,WIDE2-1::W4X-7    :hello{12",
    "N0CALL-9>T2SP0W,N4DIGI*,WIDE2-1:`c51l#T[/`\"4F}_%",
    "W4X>APRS:>status only",
    "W4X-15>APRS,A*,B*,C:!4903.50N/07201.75W#",
])
def test_ax25_roundtrip(tnc2: str) -> None:
    frame = tnc2_to_ax25(tnc2)
    assert frame[-len(tnc2.split(":", 1)[1].encode("latin-1")):] == \
        tnc2.split(":", 1)[1].encode("latin-1")
    expected = tnc2.replace("A*,B*", "A,B*")  # only the LAST used digi carries '*'
    assert ax25_to_tnc2(frame) == expected


def test_ax25_address_bytes() -> None:
    f = tnc2_to_ax25("KY4WL-5>APRS:x")
    # dest "APRS  " shifted left, SSID byte 0x60; src last-address bit set
    assert f[:6] == bytes(ord(c) << 1 for c in "APRS  ") and f[6] == 0x60
    assert f[7:13] == bytes(ord(c) << 1 for c in "KY4WL ") and f[13] == 0x60 | (5 << 1) | 1
    assert f[14:16] == b"\x03\xf0"


@pytest.mark.parametrize("bad", ["no-arrow:x", "A>B", "TOOLONGCALL>APRS:x", "W4X-16>APRS:x",
                                 "W4X>APRS," + ",".join(["WIDE1-1"] * 9) + ":x"])
def test_ax25_rejects(bad: str) -> None:
    with pytest.raises(FrameError):
        tnc2_to_ax25(bad)


def test_ax25_rejects_non_ui() -> None:
    f = bytearray(tnc2_to_ax25("W4X>APRS:x"))
    f[14] = 0x3F  # not UI
    with pytest.raises(FrameError):
        ax25_to_tnc2(bytes(f))
    with pytest.raises(FrameError):
        ax25_to_tnc2(b"\x00" * 5)


def test_kiss_escaping_and_decoder() -> None:
    payload = bytes([1, FEND, 2, FESC, 3])
    framed = kiss_frame(payload)
    assert framed[0] == FEND and framed[-1] == FEND and FEND not in framed[1:-1]
    dec = KissDecoder()
    # split across reads, plus a non-data (command 6) frame that must be ignored
    out = dec.feed(framed[:4]) + dec.feed(framed[4:] + bytes([FEND, 0x06, 9, FEND]))
    assert out == [payload]


async def test_kiss_tcp_link_against_fake_direwolf() -> None:
    received: list[str] = []
    got_from_client: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(kiss_frame(tnc2_to_ax25("W4X>APRS:>hello from RF")))
        await writer.drain()
        got_from_client.append(await reader.read(1024))
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    link = KissTcpLink("127.0.0.1", port)
    with pytest.raises(LinkDown):
        await link.send("KY4WL-5>APZRBK:>x")

    async def on_frame(t: str) -> None:
        received.append(t)

    task = asyncio.create_task(link.run(on_frame))
    for _ in range(100):
        if received:
            break
        await asyncio.sleep(0.02)
    assert received == ["W4X>APRS:>hello from RF"] and link.connected
    await link.send("KY4WL-5>APZRBK:>test")
    for _ in range(100):
        if got_from_client:
            break
        await asyncio.sleep(0.02)
    dec = KissDecoder()
    assert [ax25_to_tnc2(f) for f in dec.feed(got_from_client[0])] == ["KY4WL-5>APZRBK:>test"]
    task.cancel()
    server.close()
