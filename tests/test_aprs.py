"""APRS service: gating, stations, messaging (ack/retry/dedupe), and the API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from rigbroker.api import create_app
from rigbroker.aprs import AprsError, AprsService, bearing_distance, validate_text
from rigbroker.broker import Broker
from rigbroker.events import EventLog
from rigbroker.kiss import FrameHandler, LinkDown
from rigbroker.mock_aprs import MockAprsLink
from tests.conftest import LOGIN, auth_store, make_config, wait_until

ME = "KY4WL-5"


class FakeLink:
    connected = True

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.down = False

    async def run(self, on_frame: FrameHandler) -> None:
        await asyncio.Event().wait()

    async def send(self, tnc2: str) -> None:
        if self.down:
            raise LinkDown("down")
        self.sent.append(tnc2)


class Gates:
    def __init__(self) -> None:
        self.rx = True
        self.tx = True

    def rx_gate(self) -> tuple[bool, str]:
        return self.rx, "rx" if self.rx else "APRS not running"

    def tx_gate(self) -> tuple[bool, str]:
        return self.tx, "on air" if self.tx else "transmit disabled (--allow-tx not set)"


@pytest.fixture
def svc() -> Iterator[tuple[AprsService, FakeLink, Gates]]:
    cfg = make_config().aprs
    assert cfg is not None
    link, g = FakeLink(), Gates()
    s = AprsService(cfg, link, EventLog(), rx_gate=g.rx_gate, tx_gate=g.tx_gate,
                    position=lambda: (37.0, -86.0))
    yield s, link, g


def test_bearing_distance() -> None:
    km, brg = bearing_distance(37.0, -86.0, 38.0, -86.0)
    assert 110 < km < 112 and abs(brg) < 0.5
    km, brg = bearing_distance(37.0, -86.0, 37.0, -85.0)
    assert 85 < km < 92 and 89 < brg < 91


def test_validate_text() -> None:
    assert validate_text("  hi  ") == "hi"
    for bad in ("", "x" * 68, "no {braces", "pipe|", "tilde~", "naïve"):
        with pytest.raises(AprsError):
            validate_text(bad)


async def test_station_tracking_and_rx_gate(svc: tuple[AprsService, FakeLink, Gates]) -> None:
    s, _, g = svc
    await s.on_frame("W4X-9>APRS,N4DIGI*,WIDE2-1:!3800.00N/08600.00W>mobile 1")
    st = s.stations["W4X-9"]
    assert st.via == "N4DIGI" and st.symbol == "/>" and st.comment == "mobile 1"
    assert st.distance_km is not None and 110 < st.distance_km < 112
    await s.on_frame("W4X-9>APRS:>on the road")
    assert s.stations["W4X-9"].status == "on the road" and s.stations["W4X-9"].packets == 2
    g.rx = False  # e.g. radio switched to Winlink: Direwolf is now a packet modem
    await s.on_frame("W4Y>APRS:!3800.00N/08600.00W-")
    assert "W4Y" not in s.stations and s.dropped_while_off == 1
    await s.on_frame("garbage without format")
    g.rx = True
    await s.on_frame("garbage without format")
    assert s.packets[-1].fmt == "unparsed"


async def test_incoming_message_is_acked_once_stored_once(
        svc: tuple[AprsService, FakeLink, Gates]) -> None:
    s, link, _ = svc
    raw = f"W4X-7>APK102,WIDE1-1::{ME:<9}:hello there{{12"
    await s.on_frame(raw)
    await s.on_frame(raw)  # duplicate (heard via a digi too)
    inbox = [m for m in s.messages if m.direction == "in"]
    assert len(inbox) == 1 and inbox[0].text == "hello there" and inbox[0].mine
    assert link.sent == [f"{ME}>APZRBK,WIDE1-1,WIDE2-1::W4X-7    :ack12"] * 2  # ack each copy


async def test_message_to_other_call_shown_not_acked(
        svc: tuple[AprsService, FakeLink, Gates]) -> None:
    s, link, _ = svc
    await s.on_frame("W4X>APRS::KY4WL    :for the base call{3")
    await s.on_frame("W4X>APRS::SOMEONE  :not ours{4")
    assert [m.text for m in s.messages] == ["for the base call"]
    assert not s.messages[0].mine and link.sent == []


async def test_no_ack_when_off_air(svc: tuple[AprsService, FakeLink, Gates]) -> None:
    s, link, g = svc
    g.tx = False
    await s.on_frame(f"W4X>APRS::{ME:<9}:hi{{9")
    assert len(s.messages) == 1 and link.sent == []
    assert any("could not ack" in e.message for e in s.events.recent(0))


async def test_send_ack_flow(svc: tuple[AprsService, FakeLink, Gates]) -> None:
    s, link, _ = svc
    m = await s.send_message("w4x-7", "hi from rigbroker")
    assert link.sent == [f"{ME}>APZRBK,WIDE1-1,WIDE2-1::W4X-7    :hi from rigbroker{{{m.msgno}"]
    assert m.state == "sending" and m.tries == 1
    await s.on_frame(f"W4X-7>APRS::{ME:<9}:ack{m.msgno}")
    assert m.state == "acked"


async def test_reject_and_retry_then_fail(svc: tuple[AprsService, FakeLink, Gates]) -> None:
    s, link, _ = svc
    a = await s.send_message("W4X", "first")
    await s.on_frame(f"W4X>APRS::{ME:<9}:rej{a.msgno}")
    assert a.state == "rejected"
    b = await s.send_message("W4Y", "nobody home")
    await s.start()
    try:  # msg_retry_s=0.2, msg_retries=3 in the test config
        await wait_until(lambda: b.state == "failed", timeout=6)
    finally:
        await s.stop()
    assert b.tries == 3 and sum("nobody home" in x for x in link.sent) == 3


async def test_send_refused(svc: tuple[AprsService, FakeLink, Gates]) -> None:
    s, link, g = svc
    with pytest.raises(AprsError, match="own callsign"):
        await s.send_message(ME, "loop")
    with pytest.raises(AprsError):
        await s.send_message("W4X; reboot", "x")
    g.tx = False
    with pytest.raises(AprsError, match="--allow-tx"):
        await s.send_message("W4X", "x")
    assert link.sent == []


async def test_cancel(svc: tuple[AprsService, FakeLink, Gates]) -> None:
    s, _, _ = svc
    m = await s.send_message("W4X", "never mind")
    assert (await s.cancel(m.id)).state == "cancelled"
    with pytest.raises(AprsError):
        await s.cancel("nope")


async def test_mock_link_acks_our_messages() -> None:
    link = MockAprsLink(lambda: (37.0, -86.0), ME, interval=0.05)
    got: list[str] = []

    async def on_frame(t: str) -> None:
        got.append(t)

    link.rng.seed(1)
    link.rng.random = lambda: 0.0  # type: ignore[method-assign]  # always ack
    link.rng.uniform = lambda a, b: 0.05  # type: ignore[method-assign]
    task = asyncio.create_task(link.run(on_frame))
    await link.send(f"{ME}>APZRBK::MOCK7-5  :hello{{42")
    await wait_until(lambda: any(":ack42" in x for x in got), timeout=3)
    task.cancel()


# ---------------------------------------------------------------- broker gates + API


@pytest.fixture
def client() -> Iterator[tuple[TestClient, Broker, FakeLink]]:
    link = FakeLink()
    b = Broker.build(make_config(), mock=True, allow_tx=True, simulate=False)
    assert b.aprs is not None
    b.aprs.link = link
    store = auth_store()
    with TestClient(create_app(b, auth_store=store), headers={"X-RigBroker": "1"}) as c:
        assert c.post("/auth/login", json=LOGIN).status_code == 200
        end = time.monotonic() + 3
        while c.get("/radios/vu").json()["state"] != "default":
            assert time.monotonic() < end
            time.sleep(0.01)
        yield c, b, link


def test_aprs_api_send_and_gate(client: tuple[TestClient, Broker, FakeLink]) -> None:
    c, _, link = client
    st = c.get("/aprs").json()["status"]
    assert st["callsign"] == ME and st["tx"] is True and st["rx"] is True
    r = c.post("/aprs/messages", json={"to": "w4x-7", "text": "api test"})
    assert r.status_code == 200 and r.json()["message"]["peer"] == "W4X-7"
    assert link.sent and link.sent[-1].endswith("api test{" + r.json()["message"]["msgno"])
    assert c.post("/aprs/messages", json={"to": "W4X", "text": "x" * 80}).status_code == 422
    assert c.post("/aprs/messages", json={"to": "W4X;rm", "text": "hi"}).status_code == 422
    # switch the radio to the monitor profile: APRS consumer stops -> no TX, no RX
    assert c.post("/radios/vu/lease", json={"profile": "monitor"}).status_code == 200
    end = time.monotonic() + 3
    while c.get("/radios/vu").json()["state"] != "active":
        assert time.monotonic() < end
        time.sleep(0.01)
    st = c.get("/aprs").json()["status"]
    assert st["tx"] is False and "direwolf@aprs" in st["tx_reason"] and st["rx"] is False
    r = c.post("/aprs/messages", json={"to": "W4X", "text": "should not go"})
    assert r.status_code == 409 and "direwolf@aprs" in r.json()["reason"]


def test_aprs_requires_login(client: tuple[TestClient, Broker, FakeLink]) -> None:
    c, _, _ = client
    c.post("/auth/logout")
    assert c.get("/aprs").status_code == 200  # viewing is public
    assert c.post("/aprs/messages", json={"to": "W4X", "text": "hi"}).status_code == 401
    assert c.put("/direwolf/settings", json={"beacon": False}).status_code == 401
    assert c.post("/direwolf/apply").status_code == 401


def test_direwolf_api(client: tuple[TestClient, Broker, FakeLink]) -> None:
    c, b, _ = client
    info = c.get("/direwolf").json()
    assert info["callsign"] == ME and info["running"] == {"vu": "direwolf@aprs"}
    r = c.put("/direwolf/settings", json={"beacon": True, "lat": 37.5, "lon": -86.25,
                                          "passcode": 4242})
    assert r.status_code == 200 and r.json()["settings"]["passcode"] == "set"
    assert c.put("/direwolf/settings", json={"comment": 'a"b'}).status_code == 422
    prev = c.get("/direwolf/preview/aprs").text
    assert "PBEACON" in prev and "IGLOGIN" not in prev
    c.put("/direwolf/settings", json={"igate": True})
    prev = c.get("/direwolf/preview/aprs").text
    assert "IGLOGIN KY4WL-5 *****" in prev and "4242" not in prev  # never shown
    assert c.get("/direwolf/preview/evil").status_code == 404
    r = c.post("/direwolf/apply")
    assert r.status_code == 200 and r.json()["restarted"] == ["restarted direwolf@aprs"]
    assert ("stop", "direwolf@aprs") in b.services.history  # type: ignore[attr-defined]
    assert c.get("/radios/vu").json()["consumer"] == "direwolf@aprs"
    # station position for the map now comes from the beacon settings
    assert c.get("/aprs").json()["status"]["position"] == [37.5, -86.25]


def test_csp_allows_only_the_map_origins(client: tuple[TestClient, Broker, FakeLink]) -> None:
    c, _, _ = client
    csp = c.get("/").headers["content-security-policy"]
    assert "script-src 'self' https://unpkg.com;" in csp
    assert "img-src 'self' data: https://tile.openstreetmap.org;" in csp
    html = c.get("/").text
    assert 'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="' in html
