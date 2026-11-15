"""Airband scan + hold: cycling, hold on signal, hang time, max hold, skip, stop rules, API."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rigbroker.airband import AirbandChannels
from rigbroker.api import create_app
from rigbroker.broker import Broker
from rigbroker.mock_rig import MockRig
from rigbroker.scan import Scanner
from rigbroker.state import RadioState
from tests.conftest import LOGIN, MakeHarness, auth_store, wait_until
from tests.test_airband import airband_config
from tests.test_api import poll

A, B, C = 118_100_000, 119_200_000, 120_900_000


class BusyRig(MockRig):
    """Squelch (DCD) open only on the 'busy' frequencies."""

    def __init__(self) -> None:
        super().__init__()
        self.busy: set[int] = set()
        self.visited: list[int] = []

    async def set_freq(self, hz: int) -> None:
        await super().set_freq(hz)
        self.visited.append(int(hz))

    async def get_dcd(self) -> bool:
        await self._call("get_dcd")
        return self.freq in self.busy


async def scanner(make: MakeHarness, **cfg: Any) -> tuple[Any, BusyRig, Scanner]:
    rig = BusyRig()
    h = await make(rig=rig)
    full = airband_config(scan_dwell_s=0.1, scan_hang_s=0.3, scan_max_hold_s=0.8, **cfg)
    h.radio.cfg = full.radios["vu"]
    await h.wait_state(RadioState.DEFAULT)
    assert (await h.radio.request_lease("airband", holder="web")).ok
    await h.wait_state(RadioState.ACTIVE)
    assert full.airband is not None
    ch = AirbandChannels(full.airband, h.radio.cfg.profiles["airband"])
    ch.set_scan([A, B, C])
    sc = Scanner(h.radio, ch, full.airband, h.events)
    return h, rig, sc


async def test_cycles_through_the_list_when_quiet(make: MakeHarness) -> None:
    h, rig, sc = await scanner(make)
    ok, msg = await sc.start(by="web")
    assert ok, msg
    await wait_until(lambda: {A, B, C} <= set(rig.visited) and len(rig.visited) >= 5, 5, "cycle")
    assert sc.state == "scanning" and h.radio.applied_freq in (A, B, C)
    assert not h.messages("retune")  # scan steps don't flood the event log
    assert await sc.stop("test") and sc.state == "off"


async def test_holds_on_signal_then_resumes_after_hang(make: MakeHarness) -> None:
    _, rig, sc = await scanner(make)
    rig.busy = {B}
    await sc.start(by="web")
    await wait_until(lambda: sc.state == "holding" and sc.freq == B, 5, "hold on B")
    n = len(rig.visited)
    await wait_until(lambda: sc.status()["hold_s"] is not None, 2, "hold timer")
    assert len(rig.visited) == n and rig.freq == B
    rig.busy = set()  # transmission over -> hang time -> scan on
    await wait_until(lambda: len(rig.visited) > n, 3, "resumed")
    await sc.stop("test")


async def test_continuous_signal_moves_on_after_max_hold(make: MakeHarness) -> None:
    h, rig, sc = await scanner(make)
    rig.busy = {B}  # e.g. an ATIS that never stops
    await sc.start(by="web")
    await wait_until(lambda: sc.state == "holding", 5, "hold")
    await wait_until(lambda: any("busy for" in m for m in h.messages("scan")), 3, "max hold")
    assert rig.freq != B or sc.state == "scanning"
    await sc.stop("test")


async def test_skip_leaves_the_channel_out_until_restart(make: MakeHarness) -> None:
    _, rig, sc = await scanner(make)
    rig.busy = {B}
    await sc.start(by="web")
    await wait_until(lambda: sc.state == "holding", 5, "hold")
    assert sc.skip()
    await wait_until(lambda: B in sc.skipped, 2, "skipped")
    n = len(rig.visited)
    await wait_until(lambda: len(rig.visited) >= n + 4, 5, "more steps")
    assert B not in rig.visited[n:]  # never back on the skipped channel
    await sc.stop("test")
    await sc.start(by="web")  # a restart forgets skips
    assert not sc.skipped
    await sc.stop("test")


async def test_stops_when_the_session_ends_and_refusals(make: MakeHarness) -> None:
    h, _, sc = await scanner(make)
    assert not sc.skip()  # not holding
    await sc.start(by="web")
    await h.radio.release(by="test")
    await wait_until(lambda: bool(sc.stopped_reason), 5, "stopped")
    assert sc.state == "off"
    assert "session ended" in sc.stopped_reason
    ok, msg = await sc.start(by="web")
    assert not ok and "start listening" in msg


async def test_empty_list_refused(make: MakeHarness) -> None:
    _, _, sc = await scanner(make)
    sc.channels.set_scan([])
    ok, msg = await sc.start(by="web")
    assert not ok and "empty" in msg


# ---------------------------------------------------------------- API


@pytest.fixture
def client() -> Iterator[tuple[TestClient, Broker]]:
    cfg = airband_config(scan_dwell_s=0.1, scan_hang_s=0.3, scan_max_hold_s=0.8)
    broker = Broker.build(cfg, mock=True, allow_tx=True, simulate=False)
    with TestClient(create_app(broker, auth_store=auth_store()),
                    headers={"X-RigBroker": "1"}) as c:
        yield c, broker


def test_scan_api(client: tuple[TestClient, Broker]) -> None:
    c, broker = client
    assert c.put("/airband/scan", json={"freqs": [A, B]}).status_code == 401
    assert c.post("/auth/login", json=LOGIN).status_code == 200
    assert c.put("/airband/scan", json={"freqs": [A, B, A]}).json()["scan"] == [A, B]
    assert c.put("/airband/scan", json={"freqs": [146520000]}).status_code == 422
    assert c.get("/airband").json()["scan"] == [A, B]
    r = c.post("/airband/scan/start")
    assert r.status_code == 409 and "listening" in r.json()["reason"]
    poll(c, lambda s: s["state"] == "default")
    assert c.post("/radios/vu/lease", json={"profile": "airband"}).status_code == 200
    poll(c, lambda s: s["state"] == "active")
    assert c.post("/airband/scan/start").status_code == 200
    poll(c, lambda s: s["applied_freq"] in (A, B))
    assert c.get("/status").json()["scan"]["state"] in ("scanning", "holding")
    # tuning by hand stops the scan
    assert c.post("/radios/vu/tune", json={"freq": C, "mode": "AM"}).status_code == 200
    st = c.get("/status").json()["scan"]
    assert st["state"] == "off" and "tuned by hand" in st["stopped"]
    assert c.get("/radios/vu").json()["applied_freq"] == C
    assert c.post("/airband/scan/stop").status_code == 409  # not scanning any more
    assert broker.scanner is not None and not broker.scanner.running
