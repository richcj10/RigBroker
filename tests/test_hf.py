"""FT-891 (HF): manual sessions, web tuning inside band edges, VFO following, mic PTT,
Tune carrier for an auto-tuner, TX meters, config validation and the API."""

from __future__ import annotations

import copy
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rigbroker.api import create_app
from rigbroker.broker import Broker
from rigbroker.config import RadioConfig, parse_config
from rigbroker.events import EventLog
from rigbroker.jobs import MockJobRunner
from rigbroker.mock_rig import MockRig
from rigbroker.radio import RadioController
from rigbroker.rig_client import RigError
from rigbroker.services import MockServiceController
from rigbroker.state import RadioState
from rigbroker.telemetry import Sample
from tests.conftest import LOGIN, auth_store, make_config, wait_until
from tests.test_api import poll

HF: dict[str, Any] = {
    "name": "FT-891", "rigctld": "127.0.0.1:4543", "tab": "hf", "default_profile": "off",
    "power_levels": {"5W": 0.05, "10W": 0.10, "25W": 0.25}, "max_power_level": "25W",
    "tune": {"power": 0.10, "mode": "RTTY", "seconds": 0.2}, "tx_meters": True,
    "profiles": {
        "off": {"rig_power": "off"},
        "hf_manual": {"freq": 14200000, "mode": "USB", "power": 0.10, "manual": True,
                      "modes": ["USB", "LSB", "CW", "RTTY", "PKTUSB"],
                      "freq_ranges": [[7000000, 7300000], [14000000, 14350000]],
                      "max_minutes": 10, "transmits": True},
    },
}


class HfRig(MockRig):
    """MockRig whose PTT can refuse to release (for the Tune safety path)."""

    stuck = False

    async def set_ptt(self, on: bool) -> None:
        if not on and self.stuck:
            await self._call("set_ptt", on)
            raise RigError("set_ptt failed: RPRT -5", -5)
        await super().set_ptt(on)


Make = Callable[..., Awaitable[tuple[RadioController, HfRig, EventLog]]]


@pytest.fixture
async def hf() -> AsyncIterator[Make]:
    made: list[RadioController] = []

    async def _make(*, allow_tx: bool = True, **over: Any) -> tuple[RadioController, HfRig,
                                                                    EventLog]:
        d = copy.deepcopy(HF)
        d.update(over)
        cfg = make_config()
        rig = HfRig(freq=14_200_000, mode="USB")
        rig.tx_meters = True
        ev = EventLog(maxlen=2000)
        r = RadioController("hf", RadioConfig(**d), cfg.safety, rig, MockServiceController(),
                            MockJobRunner(), ev, allow_tx=allow_tx)
        made.append(r)
        await r.start()
        await wait_until(lambda: r.state is RadioState.DEFAULT, 3, "default (off)")
        return r, rig, ev

    yield _make
    for r in made:
        await r.stop()


async def start_manual(r: RadioController) -> None:
    assert (await r.request_lease("hf_manual", holder="web")).ok
    await wait_until(lambda: r.state is RadioState.ACTIVE, 3, "manual session active")


async def test_idle_off_then_manual_session_powers_on_and_back_off(hf: Make) -> None:
    r, rig, _ = await hf()
    assert not rig.power_on  # default profile "off"
    await start_manual(r)
    assert rig.power_on and rig.freq == 14_200_000 and r.applied_mode == "USB"
    await r.release(by="test")
    await wait_until(lambda: r.state is RadioState.DEFAULT and not rig.power_on, 3, "off again")


async def test_retune_inside_band_edges_only(hf: Make) -> None:
    r, rig, _ = await hf()
    assert not (await r.retune(7_100_000, "LSB", by="web"))[0]  # no session yet
    await start_manual(r)
    ok, msg = await r.retune(7_100_000, "LSB", by="web")
    assert ok, msg
    assert (rig.freq, rig.mode) == (7_100_000, "LSB") and r.applied_freq == 7_100_000
    assert r.band_memory.data["40m"] == {"freq": 7_100_000, "mode": "LSB"}
    ok, msg = await r.retune(14_400_000, None, by="web")  # past the 20 m edge
    assert not ok and "outside" in msg and rig.freq == 7_100_000
    ok, msg = await r.retune(None, "FM", by="web")
    assert not ok and "not allowed" in msg
    rig.ptt = True
    assert not (await r.retune(7_150_000, None, by="web"))[0]  # never while transmitting


async def test_vfo_knob_is_followed_and_mic_ptt_is_fine(hf: Make) -> None:
    r, rig, _ = await hf()
    await start_manual(r)
    rig.freq, rig.mode = 14_074_000, "PKTUSB"  # operator turns the knob
    await wait_until(lambda: r.applied_freq == 14_074_000, 3, "VFO followed")
    assert r.state is RadioState.ACTIVE and r.band_memory.data["20m"]["mode"] == "PKTUSB"
    rig.ptt = True  # operator keys the mic inside the band
    await wait_until(lambda: r.telemetry.ptt is True, 3, "PTT seen")
    await wait_until(lambda: r.telemetry.swr is not None, 3, "TX meters read")
    assert r.state is RadioState.ACTIVE and r.fault is None
    assert r.telemetry.po_w == 10.0 and r.telemetry.swr == 1.3
    rig.ptt = False


async def test_transmitting_outside_the_band_is_a_fault(hf: Make) -> None:
    r, rig, _ = await hf()
    await start_manual(r)
    rig.freq = 14_400_000  # knob past the band edge: listening is fine...
    await wait_until(lambda: r.applied_freq == 14_400_000, 3, "followed out of band")
    assert r.fault is None
    rig.ptt = True  # ...transmitting there is not
    await wait_until(lambda: r.fault is not None, 3, "fault")
    assert r.fault is not None and "outside the allowed band edges" in r.fault.reason


async def test_high_swr_warns(hf: Make) -> None:
    r, rig, ev = await hf()
    await start_manual(r)
    rig.swr = 4.2
    rig.ptt = True
    await wait_until(lambda: any("high SWR" in e.message for e in ev.recent(0, kind="swr")),
                     3, "SWR warning")
    rig.ptt = False


async def test_tune_sends_capped_carrier_and_restores(hf: Make) -> None:
    r, rig, _ = await hf()
    await start_manual(r)
    rig.levels["RFPOWER"] = 0.25
    rig.calls.clear()
    before = time.monotonic()
    ok, msg = await r.tune_atu(by="web")
    assert ok and msg == "tuned, SWR 1.3:1", msg  # meters read during the carrier
    seq = [(n, a) for n, a in rig.calls if n.startswith("set_")]
    assert seq[:3] == [("set_mode", ("RTTY", 0)), ("set_level", ("RFPOWER", 0.10)),
                       ("set_ptt", (True,))]
    assert ("set_ptt", (False,)) in seq and not rig.ptt
    assert (rig.mode, rig.levels["RFPOWER"]) == ("USB", 0.25)  # restored
    assert r.fault is None
    # a poll taken during the carrier (mode RTTY) must not be "followed" afterwards
    stale = Sample(started_mono=before, freq=rig.freq, mode="RTTY", ptt=False, dcd=False,
                   strength=None)
    await r.on_sample(stale)
    assert r.applied_mode == "USB"
    fresh = Sample(started_mono=time.monotonic(), freq=rig.freq, mode="LSB", ptt=False,
                   dcd=False, strength=None)
    await r.on_sample(fresh)  # a real knob change afterwards is still followed
    assert r.applied_mode == "LSB"


async def test_tune_refusals(hf: Make) -> None:
    r, _, _ = await hf(allow_tx=False)
    assert "--allow-tx" in (await r.tune_atu(by="web"))[1]
    r2, _, _ = await hf()
    assert "manual session" in (await r2.tune_atu(by="web"))[1]  # radio is off/idle


async def test_tune_ptt_that_wont_release_is_a_fault(hf: Make) -> None:
    r, rig, _ = await hf()
    await start_manual(r)
    rig.stuck = True
    ok, msg = await r.tune_atu(by="web")
    assert not ok and "FAULT" in msg
    await wait_until(lambda: r.fault is not None, 3, "fault")


def test_config_validation() -> None:
    d = copy.deepcopy(HF)
    d["tune"]["power"] = 0.5  # above the 25 W cap
    with pytest.raises(Exception, match="above the power cap"):
        RadioConfig(**d)
    d = copy.deepcopy(HF)
    d["profiles"]["hf_manual"]["modes"] = []
    with pytest.raises(Exception, match="manual profiles need"):
        RadioConfig(**d)
    d = copy.deepcopy(HF)
    d["profiles"]["hf_manual"]["mode"] = "AM"  # not in modes
    with pytest.raises(Exception, match="not one of this profile's modes"):
        RadioConfig(**d)


def test_api_tune_and_atu() -> None:
    d = make_config().model_dump()
    d["radios"]["hf"] = copy.deepcopy(HF)
    broker = Broker.build(parse_config(d), mock=True, allow_tx=True, simulate=False)
    with TestClient(create_app(broker, auth_store=auth_store()),
                    headers={"X-RigBroker": "1"}) as c:
        assert c.post("/radios/hf/tune", json={"freq": 7100000}).status_code == 401
        assert c.post("/auth/login", json=LOGIN).status_code == 200
        poll(c, lambda s: s["state"] == "default", rid="hf")
        r = c.post("/radios/hf/tune", json={"freq": 7100000})
        assert r.status_code == 409 and "manual session" in r.json()["reason"]
        assert c.post("/radios/hf/lease", json={"profile": "hf_manual"}).status_code == 200
        poll(c, lambda s: s["state"] == "active", rid="hf")
        r = c.post("/radios/hf/tune", json={"freq": 7100000, "mode": "lsb"})
        assert r.status_code == 200, r.json()
        st = c.get("/radios/hf").json()
        assert st["applied_freq"] == 7_100_000 and st["applied_mode"] == "LSB"
        assert st["tab"] == "hf" and st["profiles"]["hf_manual"]["manual"] is True
        assert c.post("/radios/hf/tune", json={"freq": 7400000}).status_code == 409
        r = c.post("/radios/hf/atu")
        assert r.status_code == 200, r.json()
