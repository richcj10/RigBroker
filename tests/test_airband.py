"""Airband: receive-only manual profile, squelch, channel list, audio relay and /ws/audio."""

from __future__ import annotations

import asyncio
import copy
import struct
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rigbroker.api import create_app
from rigbroker.audio import CHUNK, AudioRelay
from rigbroker.broker import Broker
from rigbroker.config import Config, parse_config
from rigbroker.state import RadioState
from tests.conftest import LOGIN, MakeHarness, auth_store, make_config, wait_until
from tests.test_api import poll

AIRBAND_PROFILE: dict[str, Any] = {
    "freq": 121500000, "mode": "AM", "squelch": 0.25, "volume": 0.12, "service": "audio-stream",
    "manual": True, "modes": ["AM"], "freq_ranges": [[118000000, 136975000]],
    "max_minutes": 10080, "transmits": False}


def airband_config(**over: Any) -> Config:
    d = make_config().model_dump()
    d["radios"]["vu"]["profiles"]["airband"] = copy.deepcopy(AIRBAND_PROFILE)
    d["airband"] = {"radio": "vu", "profile": "airband", "state_file": None,
                    "channels": [{"freq": 121500000, "name": "Guard", "group": "ALL"},
                                 {"freq": 127575000, "name": "ATIS", "group": "KTST"}]}
    d["airband"].update(over)
    return parse_config(d)


def test_config_validation() -> None:
    cfg = airband_config()
    assert cfg.airband is not None and len(cfg.airband.channels) == 2
    with pytest.raises(Exception, match="outside airband"):
        airband_config(channels=[{"freq": 145000000, "name": "nope"}])
    d = airband_config().model_dump()
    d["airband"]["profile"] = "aprs"  # transmits, not manual
    with pytest.raises(Exception, match="receive-only"):
        parse_config(d)


# ---------------------------------------------------------------- radio behaviour


async def lease_airband(make: MakeHarness) -> Any:
    h = await make()
    h.radio.cfg = airband_config().radios["vu"]  # same radio, plus the airband profile
    await h.wait_state(RadioState.DEFAULT)
    assert (await h.radio.request_lease("airband", holder="web")).ok
    await h.wait_state(RadioState.ACTIVE)
    return h


async def test_airband_tunes_am_inside_the_band_only(make: MakeHarness) -> None:
    h = await lease_airband(make)
    assert h.rig.mode == "AM" and h.rig.freq == 121_500_000
    assert "audio-stream" in h.services.active
    ok, msg = await h.radio.retune(127_575_000, "AM", by="web")
    assert ok, msg
    assert h.rig.freq == 127_575_000
    assert not (await h.radio.retune(145_000_000, "AM", by="web"))[0]
    assert not (await h.radio.retune(None, "FM", by="web"))[0]


async def test_any_ptt_during_airband_is_a_fault(make: MakeHarness) -> None:
    h = await lease_airband(make)
    h.rig.ptt = True  # nobody may transmit while listening to airband
    await wait_until(lambda: h.radio.fault is not None, 3, "fault")
    assert h.radio.fault is not None and "receive-only" in h.radio.fault.reason


async def test_squelch(make: MakeHarness) -> None:
    h = await make()
    assert not (await h.radio.set_squelch(0.4, by="web"))[0]  # APRS is not a manual session
    h2 = await lease_airband(make)
    ok, msg = await h2.radio.set_squelch(0.4, by="web")
    assert ok, msg
    assert h2.rig.levels["SQL"] == 0.4 and h2.radio.squelch == 0.4
    assert not (await h2.radio.set_squelch(1.5, by="web"))[0]


# ---------------------------------------------------------------- audio relay


@pytest.fixture
def sock_path() -> Iterator[str]:
    d = tempfile.mkdtemp(prefix="rb-audio-")
    yield str(Path(d) / "rx.sock")


def pcm(value: int, nbytes: int) -> bytes:
    return struct.pack(f"<{nbytes // 2}h", *([value] * (nbytes // 2)))


async def test_relay_drops_backlog_fans_out_and_reconnects(sock_path: str) -> None:
    conns: list[asyncio.StreamWriter] = []

    async def serve(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        conns.append(w)
        w.write(pcm(111, 64000))  # stale pipe backlog, arrives at once
        await w.drain()
        try:
            for _ in range(30):  # live audio
                await asyncio.sleep(0.05)
                w.write(pcm(222, CHUNK // 2))
                await w.drain()
        except (ConnectionError, OSError):
            pass

    srv = await asyncio.start_unix_server(serve, sock_path)
    active = True
    relay = AudioRelay(sock_path, lambda: active)
    fast, slow = relay.subscribe(), relay.subscribe()
    await relay.start()
    try:
        chunk = await asyncio.wait_for(fast.get(), 3)
        assert len(chunk) == CHUNK and struct.unpack_from("<h", chunk)[0] == 222  # no backlog
        assert relay.connected
        for _ in range(8):
            await asyncio.wait_for(fast.get(), 3)
        assert slow.qsize() <= 20  # never read: bounded, the relay kept going
        conns[0].close()  # service restarts -> relay reconnects
        await wait_until(lambda: len(conns) >= 2, 5, "reconnect")
        active = False  # profile ended -> relay disconnects and idles
        await wait_until(lambda: not relay.connected, 3, "idle")
    finally:
        await relay.stop()
        srv.close()


# ---------------------------------------------------------------- API


@pytest.fixture
def client() -> Iterator[tuple[TestClient, Broker]]:
    broker = Broker.build(airband_config(), mock=True, allow_tx=True, simulate=False)
    with TestClient(create_app(broker, auth_store=auth_store()),
                    headers={"X-RigBroker": "1"}) as c:
        yield c, broker


def test_airband_api_channels(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    d = c.get("/airband").json()
    assert d["range"] == [118000000, 136975000] and d["step_hz"] == 25000
    assert [x["name"] for x in d["channels"]] == ["Guard", "ATIS"]
    assert c.post("/airband/channels", json={"freq": 120900000, "name": "Tower"}).status_code == 401
    assert c.post("/auth/login", json=LOGIN).status_code == 200
    r = c.post("/airband/channels", json={"freq": 120900000, "name": "Tower", "group": "KTST"})
    assert r.status_code == 200
    assert "Tower" in [x["name"] for x in c.get("/airband").json()["channels"]]
    assert c.post("/airband/channels", json={"freq": 146520000, "name": "x"}).status_code == 422
    assert c.delete("/airband/channels/120900000").status_code == 200
    assert c.delete("/airband/channels/121500000").status_code == 404  # config channel stays


def test_ws_audio_needs_login_then_streams(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    # public view is on, but audio still needs a login
    with (pytest.raises(WebSocketDisconnect),
          c.websocket_connect("/ws/audio", headers={"origin": "http://testserver"}) as ws):
        ws.receive_bytes()
    assert c.post("/auth/login", json=LOGIN).status_code == 200
    poll(c, lambda s: s["state"] == "default")
    assert c.post("/radios/vu/lease", json={"profile": "airband"}).status_code == 200
    poll(c, lambda s: s["state"] == "active" and s["consumer"] == "audio-stream")
    with c.websocket_connect("/ws/audio", headers={"origin": "http://testserver"}) as ws:
        frame = ws.receive_bytes()
        assert len(frame) == CHUNK
    r = c.post("/radios/vu/tune", json={"freq": 127575000, "mode": "AM"})
    assert r.status_code == 200, r.json()
    assert c.post("/radios/vu/squelch", json={"level": 0.3}).status_code == 200
    assert c.get("/radios/vu").json()["squelch"] == 0.3
