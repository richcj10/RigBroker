"""GPS -> Direwolf position: rounding, last-known fill-in, ask-before-update."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rigbroker.api import create_app
from rigbroker.broker import Broker
from rigbroker.config import DirewolfConfig
from rigbroker.direwolf import DirewolfManager
from rigbroker.events import EventLog
from rigbroker.gps import Position
from rigbroker.position import STEP_DEG, PositionSync, blur
from tests.conftest import LOGIN, auth_store, make_config

# made-up test coordinates in South Dakota - never a real station position
EXACT = Position(lat=44.123456, lon=-100.654321, alt_m=None, utc=None, read_at=0.0)
NEAR = Position(lat=44.1245, lon=-100.6553, alt_m=None, utc=None, read_at=0.0)    # ~150 m away
FAR = Position(lat=44.2000, lon=-100.6543, alt_m=None, utc=None, read_at=0.0)     # ~8.5 km north


def on_grid(v: float) -> bool:
    # blur() keeps 6 decimals (~0.1 m), so allow 0.1 % of a 0.1' cell
    return abs(v / STEP_DEG - round(v / STEP_DEG)) < 1e-3


def test_blur_rounds_to_a_tenth_of_an_arc_minute() -> None:
    lat, lon = blur(EXACT.lat, EXACT.lon)
    assert on_grid(lat) and on_grid(lon)
    assert abs(lat - EXACT.lat) <= STEP_DEG / 2 and abs(lon - EXACT.lon) <= STEP_DEG / 2
    assert (lat, lon) != (EXACT.lat, EXACT.lon)


class Harness:
    def __init__(self, tmp: Path, *, last: tuple[float, float] | None = None) -> None:
        self.dw = DirewolfManager(DirewolfConfig(conf_dir=str(tmp / "dw"),
                                                 settings_file=str(tmp / "dw.json")), "KY4WL-5")
        self.path = tmp / "gps-last.json"
        if last:
            self.path.write_text(json.dumps({"lat": last[0], "lon": last[1], "ts": 1.0}))
        self.applied: list[bool] = []
        self.events = EventLog()

        async def apply(restart: bool) -> None:
            self.applied.append(restart)

        self.sync = PositionSync(self.dw, str(self.path), self.events, apply)


async def test_first_fix_fills_direwolf_without_asking(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.sync.on_fix(EXACT)
    assert h.dw.settings.lat == blur(EXACT.lat, EXACT.lon)[0] and h.sync.pending is None
    assert h.applied == [False]  # beacon off: config rewritten, nothing restarted
    # the exact position is never written anywhere
    for f in (h.path, tmp_path / "dw.json"):
        assert "123456" not in f.read_text() and "654321" not in f.read_text()


async def test_startup_uses_last_known_position(tmp_path: Path) -> None:
    h = Harness(tmp_path, last=(EXACT.lat, EXACT.lon))
    await h.sync.startup()
    assert (h.dw.settings.lat, h.dw.settings.lon) == blur(EXACT.lat, EXACT.lon)
    assert h.applied == [False]
    h2 = Harness(tmp_path)  # Direwolf already has a position now: startup leaves it alone
    await h2.sync.startup()
    assert h2.applied == []


async def test_small_wander_does_not_ask(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.sync.on_fix(EXACT)
    await h.sync.on_fix(NEAR)
    assert h.sync.pending is None and h.sync.status()["pending"] is None


async def test_real_move_asks_then_accept_updates(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.sync.on_fix(EXACT)
    before = (h.dw.settings.lat, h.dw.settings.lon)
    await h.sync.on_fix(FAR)
    assert (h.dw.settings.lat, h.dw.settings.lon) == before  # nothing changed on its own
    st = h.sync.status()["pending"]
    assert st is not None and 8 < st["distance_km"] < 9 and len(st["grid"]) == 6
    assert "lat" not in json.dumps(h.sync.status())  # status carries no coordinates
    h.dw.update({"beacon": True})
    assert await h.sync.accept()
    assert (h.dw.settings.lat, h.dw.settings.lon) == blur(FAR.lat, FAR.lon)
    assert h.applied[-1] is True  # beacon on: restart the APRS Direwolf
    assert not await h.sync.accept()  # nothing pending any more


async def test_dismiss_is_remembered_until_the_position_changes_again(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.sync.on_fix(EXACT)
    await h.sync.on_fix(FAR)
    assert h.sync.dismiss() and h.sync.pending is None
    await h.sync.on_fix(FAR)  # same place again: don't nag
    assert h.sync.pending is None
    other = Position(lat=44.30, lon=-100.6543, alt_m=None, utc=None, read_at=0.0)
    await h.sync.on_fix(other)  # moved somewhere new: ask again
    assert h.sync.pending == blur(other.lat, other.lon)


def test_api_accept_dismiss() -> None:
    broker = Broker.build(make_config(), mock=True, allow_tx=True, simulate=False)
    with TestClient(create_app(broker, auth_store=auth_store()),
                    headers={"X-RigBroker": "1"}) as c:
        assert c.post("/gps/direwolf/accept").status_code == 401  # admin only
        assert c.post("/auth/login", json=LOGIN).status_code == 200
        assert c.post("/gps/direwolf/accept").status_code == 409  # nothing pending
        ps = broker.position_sync
        assert ps is not None
        assert c.portal is not None
        c.portal.call(ps.on_fix, EXACT)
        c.portal.call(ps.on_fix, FAR)
        gps = c.get("/status").json()["gps"]
        assert gps["pending"] is not None and gps["direwolf_set"]
        assert c.post("/gps/direwolf/dismiss").status_code == 200
        assert c.get("/status").json()["gps"]["pending"] is None


@pytest.mark.parametrize("value", [0.0, 44.42, -100.07])
def test_blur_is_stable(value: float) -> None:
    lat, lon = blur(value, value)
    assert blur(lat, lon) == (lat, lon)
