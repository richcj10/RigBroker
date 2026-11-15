"""Packet BBS terminal: AGW framing, sessions (direct / via digipeater), safety, API."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rigbroker.api import create_app
from rigbroker.bbs import (
    HDR_LEN,
    AgwDecoder,
    AgwFrame,
    BbsError,
    MockAgwServer,
    clean_rx,
    clean_tx,
    via_data,
)
from rigbroker.broker import Broker
from rigbroker.config import parse_config
from tests.conftest import LOGIN, auth_store, make_config
from tests.test_api import poll, state_is

# ---------------------------------------------------------------- framing / text


def test_agw_frame_roundtrip_and_partial_feed() -> None:
    fr = AgwFrame("D", "KY4WL", "N0CALL-7", b"L\r", pid=0xF0)
    raw = fr.encode()
    assert len(raw) == HDR_LEN + 2 and raw[4:5] == b"D" and raw[6] == 0xF0
    dec = AgwDecoder()
    assert dec.feed(raw[:10]) == []  # header split across reads
    out = dec.feed(raw[10:] + AgwFrame("d", "N0CALL-7", "KY4WL").encode())
    assert [(f.kind, f.call_from, f.call_to, f.data) for f in out] == [
        ("D", "KY4WL", "N0CALL-7", b"L\r"), ("d", "N0CALL-7", "KY4WL", b"")]


def test_via_data() -> None:
    d = via_data(["N0DIGI-6"])
    assert d[0] == 1 and d[1:11] == b"N0DIGI-6\0\0" and len(d) == 11


def test_clean_rx_and_tx() -> None:
    assert clean_rx(b"Hi\r\nthere\rBBS>\x07\x1b[2J") == "Hi\nthere\nBBS>[2J"
    assert clean_tx("R 12") == "R 12"
    for bad in ("x" * 251, "café", "tab\there"):
        with pytest.raises(BbsError):
            clean_tx(bad)


def test_config_validation() -> None:
    d = make_config().model_dump()
    d["bbs"]["stations"][0]["freq"] = 7101000  # outside packet_bbs freq_ranges
    with pytest.raises(Exception, match="outside packet_bbs"):
        parse_config(d)
    d = make_config().model_dump()
    d["bbs"]["profile"] = "vara_fm"  # wrong service
    with pytest.raises(Exception, match="direwolf@pkt"):
        parse_config(d)
    assert make_config().bbs_mycall == "KY4WL"  # APRS call without SSID


# ---------------------------------------------------------------- sessions via the API


@pytest.fixture
def client() -> Iterator[tuple[TestClient, Broker]]:
    broker = Broker.build(make_config(), mock=True, allow_tx=True, simulate=False)
    store = auth_store()
    with TestClient(create_app(broker, auth_store=store), headers={"X-RigBroker": "1"}) as c:
        assert c.post("/auth/login", json=LOGIN).status_code == 200
        poll(c, state_is("default"))
        assert broker._mock_agw is not None
        broker._mock_agw.connect_delay = 0.1
        yield c, broker


def bbs_poll(c: TestClient, pred: Callable[[dict[str, Any]], bool],
             timeout: float = 5.0) -> dict[str, Any]:
    import time
    end = time.monotonic() + timeout
    while True:
        st = c.get("/bbs").json()
        if pred(st):
            return st
        assert time.monotonic() < end, f"timed out; last: {st['state']} {st['last']}"
        time.sleep(0.05)


def text_of(st: dict[str, Any]) -> str:
    return "".join(ch["text"] for ch in st["scrollback"])


def test_snapshot_lists_configured_bbs(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    st = c.get("/bbs").json()
    assert st["state"] == "idle" and st["mycall"] == "KY4WL"
    assert st["stations"][0]["call"] == "N0CALL-7" and st["stations"][0]["via"] == ["N0DIGI-6"]
    assert c.get("/status").json()["bbs"]["state"] == "idle"


def test_direct_session_send_and_disconnect(client: tuple[TestClient, Broker]) -> None:
    c, broker = client
    r = c.post("/bbs/connect", json={"call": "n0call-7"})
    assert r.status_code == 200, r.json()
    st = bbs_poll(c, lambda s: s["state"] == "connected")
    assert "Hello KY4WL" in text_of(st) and st["session"]["holder"] == "admin"
    radio = c.get("/radios/vu").json()
    assert radio["applied_freq"] == 145_010_000 and radio["consumer"] == "direwolf@pkt"
    agw = broker._mock_agw
    assert agw is not None
    assert [f.kind for f in agw.received][:2] == ["X", "C"]
    assert agw.received[1].call_from == "KY4WL" and agw.received[1].call_to == "N0CALL-7"

    assert c.post("/bbs/send", json={"text": "L"}).status_code == 200
    st = bbs_poll(c, lambda s: "Msg#" in text_of(s))
    assert any(f.kind == "D" and f.data == b"L\r" for f in agw.received)
    assert any(ch["dir"] == "tx" and ch["text"] == "L\n" for ch in st["scrollback"])

    assert c.post("/bbs/disconnect").status_code == 200
    st = bbs_poll(c, lambda s: s["state"] == "idle")
    assert agw.received[-1].kind == "d" and st["last"]["call"] == "N0CALL-7"
    poll(c, state_is("default"))  # radio given back (APRS)


def test_via_digipeater(client: tuple[TestClient, Broker]) -> None:
    c, broker = client
    r = c.post("/bbs/connect", json={"call": "N0CALL-7", "via": ["n0digi-6"]})
    assert r.status_code == 200 and r.json()["session"]["via"] == ["N0DIGI-6"]
    bbs_poll(c, lambda s: s["state"] == "connected")
    agw = broker._mock_agw
    assert agw is not None
    v = next(f for f in agw.received if f.kind == "v")
    assert v.call_to == "N0CALL-7" and v.data == via_data(["N0DIGI-6"])
    c.post("/bbs/disconnect")
    bbs_poll(c, lambda s: s["state"] == "idle")


def test_remote_bye_returns_the_radio(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    c.post("/bbs/connect", json={"call": "N0CALL-7"})
    bbs_poll(c, lambda s: s["state"] == "connected")
    c.post("/bbs/send", json={"text": "B"})
    st = bbs_poll(c, lambda s: s["state"] == "idle")
    assert st["last"]["reason"] == "disconnected" and "73!" in text_of(st)
    poll(c, state_is("default"))


def test_bbs_that_never_answers(client: tuple[TestClient, Broker]) -> None:
    c, broker = client
    assert broker._mock_agw is not None
    broker._mock_agw.answer = False  # Direwolf gives up (RETRYOUT)
    c.post("/bbs/connect", json={"call": "N0CALL-7"})
    st = bbs_poll(c, lambda s: s["state"] == "idle" and s["last"] is not None)
    assert "did not answer" in st["last"]["reason"]
    poll(c, state_is("default"))


def test_refusals(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    assert c.post("/bbs/connect", json={"call": "N0PE-1"}).status_code == 409  # unknown
    assert c.post("/bbs/connect", json={"call": "bad;id"}).status_code == 422
    assert c.post("/bbs/connect", json={"call": "N0CALL-7", "via": ["x y"]}).status_code == 422
    r = c.post("/bbs/send", json={"text": "L"})
    assert r.status_code == 409 and "not connected" in r.json()["reason"]
    assert c.post("/bbs/disconnect").status_code == 409
    c.post("/bbs/connect", json={"call": "N0CALL-7"})
    r = c.post("/bbs/connect", json={"call": "N0CALL-7"})
    assert r.status_code == 409 and "already" in r.json()["reason"]
    bbs_poll(c, lambda s: s["state"] == "connected")
    r = c.post("/bbs/send", json={"text": "café"})
    assert r.status_code == 409 and "ASCII" in r.json()["reason"]
    c.post("/bbs/disconnect")
    bbs_poll(c, lambda s: s["state"] == "idle")


def test_station_list_admin(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    r = c.post("/bbs/stations", json={"call": "n7xyz-4", "freq": 441300000, "name": "Test",
                                      "via": []})
    assert r.status_code == 200 and r.json()["station"]["call"] == "N7XYZ-4"
    assert "N7XYZ-4" in [s["call"] for s in c.get("/bbs").json()["stations"]]
    r = c.post("/bbs/stations", json={"call": "N7HF-1", "freq": 7101000})
    assert r.status_code == 422 and "outside" in r.json()["detail"]
    assert c.delete("/bbs/stations/N7XYZ-4").status_code == 200
    assert c.delete("/bbs/stations/N0CALL-7").status_code == 404  # config entries stay


def test_read_only_broker_refuses() -> None:
    broker = Broker.build(make_config(), mock=True, allow_tx=False, simulate=False)
    with TestClient(create_app(broker, auth_store=auth_store()),
                    headers={"X-RigBroker": "1"}) as c:
        assert c.post("/auth/login", json=LOGIN).status_code == 200
        r = c.post("/bbs/connect", json={"call": "N0CALL-7"})
        assert r.status_code == 409 and "read-only" in r.json()["reason"]


def test_login_required_to_connect() -> None:
    broker = Broker.build(make_config(), mock=True, allow_tx=True, simulate=False)
    with TestClient(create_app(broker, auth_store=auth_store()),
                    headers={"X-RigBroker": "1"}) as c:
        assert c.post("/bbs/connect", json={"call": "N0CALL-7"}).status_code == 401
        assert c.get("/bbs").status_code == 200  # public view (config default)


def test_ws_pushes_bbs_updates(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        c.post("/bbs/connect", json={"call": "N0CALL-7"})
        kinds = set()
        for _ in range(200):
            m = ws.receive_json()
            if m["type"] == "bbs":
                kinds.add(m["kind"])
                if m["kind"] == "text" and "Hello" in m["data"]["text"]:
                    break
        assert {"status", "text"} <= kinds
    c.post("/bbs/disconnect")
    bbs_poll(c, lambda s: s["state"] == "idle")


async def test_mock_server_standalone() -> None:
    """The mock speaks enough AGW for the UI (used by --mock)."""
    import asyncio

    from rigbroker.bbs import AgwConn

    srv = MockAgwServer(connect_delay=0)
    await srv.start()
    try:
        conn = await AgwConn.open("127.0.0.1", srv.port)
        await conn.send(AgwFrame("X", "KY4WL"))
        await conn.send(AgwFrame("C", "KY4WL", "N0CALL-7"))
        kinds = [(await asyncio.wait_for(conn.recv(), 2)).kind for _ in range(3)]
        assert kinds == ["X", "C", "D"]
        await conn.close()
    finally:
        await srv.stop()
