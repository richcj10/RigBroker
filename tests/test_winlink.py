"""Gateway planner: ranking, favorites, blocklist, persistence, and fallback inside a lease."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rigbroker.config import Profile, parse_config
from rigbroker.jobs import MockJobRunner
from rigbroker.leases import PlanStep, Verdict
from rigbroker.state import RadioState as S
from rigbroker.winlink import (
    GatewayStore,
    Ranked,
    WinlinkPlanner,
    band_of,
    distance_km,
    grid_to_latlon,
    sun_elevation,
)
from tests.conftest import BASE, MakeHarness, make_config, wait_until

NIGHT = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)  # ~01:00 local in EM85 (US east)
DAY = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)  # ~13:00 local


def planner(state: str | None = None, now: datetime = DAY) -> WinlinkPlanner:
    cfg = make_config()
    return WinlinkPlanner(cfg.winlink, GatewayStore(state), now=lambda: now)


def gw_profile() -> Profile:
    return make_config().radios["vu"].profiles["winlink_gw"]


def calls(p: WinlinkPlanner) -> list[str]:
    return [s.key for s in p.plan(gw_profile())]


# ---------------------------------------------------------------- geo helpers

def test_geo_helpers() -> None:
    lat, lon = grid_to_latlon("FN31pr")
    assert abs(lat - 41.7) < 0.1 and abs(lon - (-72.7)) < 0.1
    assert distance_km("EM85ab", "EM85ab") < 1
    assert 170 < distance_km("EM85", "EM75") < 195  # one square west = 2 deg lon at ~35N
    assert band_of(7_101_000) == "40m" and band_of(145_050_000) == "2m"
    assert band_of(100_000_000) is None
    noon = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
    assert sun_elevation(0, 0, noon) > 80
    assert sun_elevation(0, 0, noon.replace(hour=0)) < -80


def test_day_night() -> None:
    assert planner(now=DAY).day_or_night() == "day"
    assert planner(now=NIGHT).day_or_night() == "night"


# ---------------------------------------------------------------- ranking

def test_favorite_first_then_distance() -> None:
    p = planner()
    assert calls(p) == ["W4FAV-10", "K4NEAR-10", "K4FAR-10"]
    ranked = p.rank(gw_profile())
    hf = next(r for r in ranked if r.gateway.call == "W4HF")
    # gateways of other modes (VARA HF / VARA FM) can't be used by this packet profile:
    # all of them sort after every usable one
    usable = [r.usable for r in ranked]
    assert not hf.usable and usable == sorted(usable, reverse=True)
    assert any("favorite" in x for x in ranked[0].reasons)


def test_history_beats_distance() -> None:
    p = planner()
    for _ in range(5):
        p.record("K4FAR-10", True, 20)
    for _ in range(3):
        p.record("K4NEAR-10", False, 60, "timed out")
    order = calls(p)
    assert order.index("K4FAR-10") < order.index("K4NEAR-10")
    near = next(r for r in p.rank(gw_profile()) if r.gateway.call == "K4NEAR-10")
    assert "last 2 attempts failed -30" in near.reasons
    assert near.stats.attempts == 3 and near.stats.successes == 0


def test_hf_band_preference_by_day_night() -> None:
    def ranked(now: datetime, call: str) -> Ranked:
        p = planner(now=now)
        p.add_gateway("W4TWNY", 14_105_000, "vara_hf", favorite=False)
        return next(r for r in p.rank() if r.gateway.call == call)

    # 20m is a day band only (default preference): +20 by day, nothing at night
    assert "20m preferred at day +20" in ranked(DAY, "W4TWNY").reasons
    assert ranked(DAY, "W4TWNY").score == ranked(NIGHT, "W4TWNY").score + 20
    # 40m is in both lists
    assert "40m preferred at night +20" in ranked(NIGHT, "W4HF").reasons
    # VHF never gets a day/night bonus
    assert not any("preferred" in x for x in ranked(DAY, "K4NEAR-10").reasons)


def test_blocked_never_planned() -> None:
    p = planner()
    p.set_blocked("w4fav-10", True)
    assert "W4FAV-10" not in calls(p)
    p.set_blocked("W4FAV-10", False)
    assert calls(p)[0] == "W4FAV-10"
    with pytest.raises(KeyError):
        p.set_blocked("K9NONE-1", True)


def test_unfavorite_config_gateway() -> None:
    p = planner()
    p.set_favorite("W4FAV-10", False)
    assert calls(p)[0] == "K4NEAR-10"


def test_plan_steps() -> None:
    p = planner()
    steps = p.plan(gw_profile())
    assert len(steps) == 3
    assert steps[0] == PlanStep("W4FAV-10", 145_050_000, "pat connect ax25:///W4FAV-10",
                                "W4FAV-10", 60.0)
    only = p.plan(gw_profile(), "k4far-10")
    assert [s.key for s in only] == ["K4FAR-10"]
    assert p.plan(gw_profile(), "W4HF") == []  # wrong mode for this profile


def test_add_gateway_validates_and_rejects_injection() -> None:
    p = planner()
    gw = p.add_gateway("ka4new-5", 146_010_000, "packet", "em85", "test")
    assert gw.call == "KA4NEW-5" and gw.grid == "EM85"
    assert p.is_favorite("KA4NEW-5")
    assert "KA4NEW-5" in calls(p)[:2]  # both favorites lead; distance decides between them
    for bad in ("W4X; rm -rf /", "W4X$(id)", "../etc", ""):
        with pytest.raises(ValueError):
            p.add_gateway(bad, 145_000_000, "packet")
    with pytest.raises(ValueError):
        p.add_gateway("W4OK-1", 145_000_000, "packet", grid="ZZ99")
    p.remove_gateway("KA4NEW-5")
    with pytest.raises(KeyError):
        p.remove_gateway("W4FAV-10")  # config gateways can only be blocked


def test_out_of_range_user_gateway_is_not_usable() -> None:
    p = planner()
    p.add_gateway("W4UHF-1", 441_000_000, "packet")
    r = next(x for x in p.rank(gw_profile()) if x.gateway.call == "W4UHF-1")
    assert not r.usable and "outside profile freq_ranges" in r.reasons[-1]
    assert "W4UHF-1" not in calls(p)


def test_persistence_roundtrip(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "winlink.json"
    p = planner(str(f))
    p.set_favorite("K4FAR-10", True)
    p.set_blocked("K4NEAR-10", True)
    p.add_gateway("W4USR-2", 145_030_000, "packet", note="mine")
    p.record("K4FAR-10", True, 12.0, "ok")
    q = planner(str(f))
    assert q.is_favorite("K4FAR-10") and "K4NEAR-10" in q.store.blocked
    assert "W4USR-2" in {g.call for g in q.gateways()}
    assert q.store.stats["K4FAR-10"].successes == 1
    assert json.loads(f.read_text())["favorites"]["K4FAR-10"] is True


def test_corrupt_state_file_is_moved_aside(tmp_path: Path) -> None:
    f = tmp_path / "winlink.json"
    f.write_text("{not json")
    p = planner(str(f))
    assert p.store.stats == {}
    assert list(tmp_path.glob("winlink.corrupt-*"))


# ---------------------------------------------------------------- inside a lease

async def test_fallback_to_next_gateway_in_same_lease(make: MakeHarness) -> None:
    h = await make(jobs=MockJobRunner(script=["fail", "success"], duration_s=0.3))
    seen: list[tuple[str, bool]] = []
    h.radio.attempt_listener = lambda rid, step, ok, d, why: seen.append((step.key, ok))
    await h.wait_state(S.DEFAULT)
    plan = planner().plan(gw_profile())
    r = await h.radio.request_lease("winlink_gw", holder="web", plan=plan)
    assert r.verdict is Verdict.GRANT
    await wait_until(lambda: h.rig.freq == 145_070_000, what="retuned to 2nd gateway")
    await h.wait_state(S.ACTIVE)
    lease = h.radio.lease
    assert lease is not None and lease.attempt == 1 and lease.id == r.lease.id  # type: ignore[union-attr]
    await h.wait_state(S.DEFAULT, timeout=4)
    assert seen == [("W4FAV-10", False), ("K4NEAR-10", True)]
    assert h.jobs.runs == ["pat connect ax25:///W4FAV-10", "pat connect ax25:///K4NEAR-10"]
    assert ("active", "switching") in h.transitions()
    assert h.messages("plan_next")
    assert "job_done" in h.messages("lease_ended")[-1]
    assert h.radio.fault is None  # drift check used the gateway freq, not the profile freq


async def test_all_gateways_fail_returns_to_default(make: MakeHarness) -> None:
    h = await make(jobs=MockJobRunner(behavior="fail", duration_s=0.05))
    await h.wait_state(S.DEFAULT)
    plan = planner().plan(gw_profile())
    assert (await h.radio.request_lease("winlink_gw", holder="web", plan=plan)).ok
    await wait_until(lambda: len(h.jobs.runs) == 3, timeout=6)
    await h.wait_state(S.DEFAULT, timeout=4)
    assert "job_failed" in h.messages("lease_ended")[-1]
    assert h.rig.freq == 144_390_000


async def test_busy_gateway_channel_tries_next(make: MakeHarness) -> None:
    h = await make(jobs=MockJobRunner(duration_s=5))
    await h.wait_state(S.DEFAULT)
    h.rig.dcd = True
    plan = planner().plan(gw_profile())
    assert (await h.radio.request_lease("winlink_gw", holder="web", plan=plan)).ok
    await wait_until(lambda: bool(h.messages("plan_next")), what="moved to next gateway")
    h.rig.dcd = False
    await h.wait_state(S.ACTIVE)
    await wait_until(lambda: bool(h.jobs.runs), what="job started")
    assert h.rig.freq == 145_070_000 and h.jobs.runs == ["pat connect ax25:///K4NEAR-10"]


def remote_profile() -> Profile:
    """winlink_gw without a run job = Winlink client on another computer."""
    return gw_profile().model_copy(update={"run": None})


def test_remote_session_plan_is_one_gateway() -> None:
    steps = planner().plan(remote_profile())
    assert [s.key for s in steps] == ["W4FAV-10"] and steps[0].run == ""
    assert [s.key for s in planner().plan(remote_profile(), "K4FAR-10")] == ["K4FAR-10"]


async def test_remote_session_holds_radio_until_release(make: MakeHarness) -> None:
    h = await make(jobs=MockJobRunner())
    await h.wait_state(S.DEFAULT)
    # swap the profile to remote mode for this radio (no run job)
    profs = dict(h.radio.cfg.profiles)
    profs["winlink_gw"] = remote_profile()
    h.radio.cfg = h.radio.cfg.model_copy(update={"profiles": profs})
    plan = planner().plan(remote_profile(), "K4FAR-10")
    assert (await h.radio.request_lease("winlink_gw", holder="web", plan=plan)).ok
    await h.wait_state(S.ACTIVE)
    assert h.rig.freq == 145_010_000 and h.consumers() == {"direwolf@pkt"}
    await asyncio.sleep(0.3)
    assert h.radio.state is S.ACTIVE and h.jobs.runs == []  # nothing run on the Pi
    await h.radio.release()
    await h.wait_state(S.DEFAULT)
    assert h.consumers() == {"direwolf@aprs"}


def test_config_allows_remote_winlink_profile() -> None:
    d = copy.deepcopy(BASE)
    del d["radios"]["vu"]["profiles"]["winlink_gw"]["run"]
    d["security"] = {"auth_file": None}
    assert parse_config(d).radios["vu"].profiles["winlink_gw"].run is None


async def test_plan_validation(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    bad = [PlanStep("W4UHF-1", 441_000_000, "pat connect ax25:///W4UHF-1", "W4UHF-1", 60)]
    r = await h.radio.request_lease("winlink_gw", holder="web", plan=bad)
    assert r.invalid and "outside the allowed ranges" in r.reason
    r = await h.radio.request_lease("winlink_gw", holder="web")
    assert r.invalid and "needs a gateway plan" in r.reason
    r = await h.radio.request_lease("monitor", holder="web", plan=bad)
    assert r.invalid and "no freq_ranges" in r.reason


async def test_without_allow_tx_no_gateway_is_called(make: MakeHarness) -> None:
    h = await make(allow_tx=False)
    await h.wait_state(S.DEFAULT)
    plan = planner().plan(gw_profile())
    r = await h.radio.request_lease("winlink_gw", holder="web", plan=plan)
    assert not r.ok and "read-only" in r.reason
    assert h.radio.state is S.DEFAULT and h.rig.freq == 144_390_000  # radio never moved
    assert h.jobs.runs == []
