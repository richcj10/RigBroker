"""REST + WebSocket tests: real FastAPI app, mock broker (no simulator randomness)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rigbroker.api import create_app
from rigbroker.auth import AuthStore
from rigbroker.broker import Broker
from tests.conftest import LOGIN, auth_store, make_config


def poll(c: TestClient, pred: Callable[[dict[str, Any]], bool], timeout: float = 3.0,
         rid: str = "vu") -> dict[str, Any]:
    end = time.monotonic() + timeout
    while True:
        st: dict[str, Any] = c.get(f"/radios/{rid}").json()
        if pred(st):
            return st
        if time.monotonic() > end:
            raise AssertionError(f"timeout; last state {st['state']}")
        time.sleep(0.01)


def state_is(s: str) -> Callable[[dict[str, Any]], bool]:
    return lambda st: st["state"] == s


@pytest.fixture
def client() -> Iterator[tuple[TestClient, Broker]]:
    """Logged-in browser-like client (session cookie + X-RigBroker header)."""
    broker = Broker.build(make_config(), mock=True, allow_tx=True, simulate=False)
    store = auth_store()
    with TestClient(create_app(broker, auth_store=store), headers={"X-RigBroker": "1"}) as c:
        assert c.post("/auth/login", json=LOGIN).status_code == 200
        poll(c, state_is("default"))
        yield c, broker


def test_status(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    st = c.get("/status").json()
    assert st["mock"] is True and st["allow_tx"] is True
    assert list(st["radios"]) == ["vu"] and st["disabled_radios"] == ["hf"]
    vu = st["radios"]["vu"]
    assert vu["applied_profile"] == "aprs" and vu["winlink_profile"] == "winlink_gw"
    assert c.get("/radios/nope").status_code == 404


def test_lease_release_cycle(client: tuple[TestClient, Broker]) -> None:
    c, broker = client
    r = c.post("/radios/vu/lease", json={"profile": "monitor", "minutes": 5})
    assert r.status_code == 200 and r.json()["verdict"] == "granted"
    st = poll(c, state_is("active"))
    assert st["lease"]["holder"] == "admin" and st["lease"]["duration_s"] == 300
    assert st["telemetry"]["freq"] in (None, 144_390_000, 147_240_000)
    assert broker.mock_rig("vu").freq == 147_240_000
    r = c.post("/radios/vu/release")
    assert r.status_code == 200 and r.json()["ok"] is True
    poll(c, state_is("default"))
    r = c.post("/radios/vu/release")
    assert r.status_code == 409 and "no active lease" in r.json()["reason"]


def test_conflict_is_409_with_reason(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    assert c.post("/radios/vu/lease", json={"profile": "monitor", "holder": "a"}).status_code \
        == 200
    poll(c, state_is("active"))
    # same logged-in user = same holder, so conflict on priority instead
    r = c.post("/radios/vu/lease", json={"profile": "vara_fm", "priority": "scheduled"})
    assert r.status_code == 409
    assert r.json()["verdict"] == "rejected" and "not preemptible" in r.json()["reason"]
    r = c.post("/radios/vu/lease", json={"profile": "vara_fm", "priority": "scheduled",
                                          "queue": True})
    assert r.status_code == 202 and r.json()["verdict"] == "queued"
    assert len(c.get("/radios/vu").json()["queue"]) == 1


@pytest.mark.parametrize("body", [
    {"profile": "nope"},
    {"profile": "aprs"},
    {"profile": "monitor", "priority": "urgent"},
])
def test_invalid_lease_is_422(client: tuple[TestClient, Broker], body: dict[str, Any]) -> None:
    c, _ = client
    r = c.post("/radios/vu/lease", json=body)
    assert r.status_code == 422 and r.json()["reason"]


def test_schema_validation_is_422(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    assert c.post("/radios/vu/lease", json={"profile": "monitor", "minutes": -1}).status_code \
        == 422
    assert c.post("/radios/vu/lease", json={}).status_code == 422


def test_winlink_check(client: tuple[TestClient, Broker]) -> None:
    c, broker = client
    broker.jobs.duration_s = 30  # type: ignore[attr-defined]
    r = c.post("/radios/vu/winlink/check", json={"holder": "ha"})
    assert r.status_code == 200, r.json()
    lease = r.json()["lease"]
    assert lease["profile"] == "winlink_gw"
    assert [s["key"] for s in lease["plan"]] == ["W4FAV-10", "K4NEAR-10", "K4FAR-10"]
    st = poll(c, state_is("active"))
    assert st["lease"]["holder"] == "admin" and st["consumer"] == "direwolf@pkt"
    assert st["applied_freq"] == 145_050_000
    # a lower-priority request can't take it over (the same user could replace it)
    assert c.post("/radios/vu/winlink/check", json={"priority": "scheduled"}).status_code == 409


def test_winlink_check_single_gateway(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    r = c.post("/radios/vu/winlink/check", json={"gateway": "k4far-10"})
    assert r.status_code == 200
    assert [s["key"] for s in r.json()["lease"]["plan"]] == ["K4FAR-10"]
    poll(c, lambda s: s["applied_freq"] == 145_010_000)


def test_winlink_check_no_usable_gateway(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    r = c.post("/radios/vu/winlink/check", json={"gateway": "W4HF"})  # vara_hf: no such profile
    assert r.status_code == 422 and "no vara_hf profile" in r.json()["reason"]
    # forcing the packet profile onto it still goes through the planner's usability check
    r = c.post("/radios/vu/winlink/check", json={"gateway": "W4HF", "profile": "winlink_gw"})
    assert r.status_code == 409 and "not usable" in r.json()["reason"]
    assert c.post("/radios/vu/winlink/check", json={"gateway": "bad;id"}).status_code == 422
    assert c.post("/radios/vu/winlink/check", json={"gateway": "NOPE-1"}).status_code == 422
    r = c.post("/radios/vu/winlink/check", json={"profile": "monitor"})
    assert r.status_code == 422 and "not a Winlink profile" in r.json()["reason"]


def test_vara_fm_gateway_tunes_the_vara_profile(client: tuple[TestClient, Broker]) -> None:
    """Picking a VARA FM gateway starts the VARA FM profile on that gateway's frequency
    (Winlink Express can't retune the radio, so the web UI does)."""
    c, _ = client
    r = c.post("/radios/vu/winlink/check", json={"gateway": "n7uhf-2"})
    assert r.status_code == 200, r.json()
    lease = r.json()["lease"]
    assert lease["profile"] == "vara_gw"
    assert [(s["key"], s["freq"]) for s in lease["plan"]] == [("N7UHF-2", 441_300_000)]
    st = poll(c, state_is("active"))
    assert st["applied_freq"] == 441_300_000 and st["consumer"] == "vara-fm"


def test_vara_profile_button_picks_best_vara_gateway(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    r = c.post("/radios/vu/winlink/check", json={"profile": "vara_gw"})
    assert r.status_code == 200, r.json()
    plan = r.json()["lease"]["plan"]
    assert len(plan) == 1 and plan[0]["key"] in ("N7VFM-2", "N7UHF-2")  # remote: one gateway


def test_gateway_panel_usable_per_mode(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    gws = {g["call"]: g for g in c.get("/winlink/gateways?radio=vu").json()["gateways"]}
    assert gws["N7VFM-2"]["usable"] and gws["N7UHF-2"]["usable"] and gws["K4FAR-10"]["usable"]
    assert not gws["W4HF"]["usable"] and "no vara_hf profile" in gws["W4HF"]["reasons"][-1]


def test_gateway_management(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    gws = c.get("/winlink/gateways?radio=vu").json()
    assert gws["grid"] == "EM85ab" and gws["period"] in ("day", "night")
    order = [g["call"] for g in gws["gateways"]]
    assert order[0] == "W4FAV-10" and order[-1] == "W4HF"
    assert gws["gateways"][-1]["usable"] is False

    assert c.post("/winlink/gateways/K4FAR-10/favorite", json={"value": True}).status_code == 200
    assert c.post("/winlink/gateways/W4FAV-10/block", json={"value": True}).status_code == 200
    order = [g["call"] for g in c.get("/winlink/gateways?radio=vu").json()["gateways"]]
    assert order[0] == "K4FAR-10"
    assert c.post("/winlink/gateways/NOPE-1/favorite", json={"value": True}).status_code == 404

    r = c.post("/winlink/gateways", json={"call": "kb4usr-3", "freq": 145030000,
                                          "mode": "packet", "grid": "EM85", "note": "mine"})
    assert r.status_code == 200 and r.json()["call"] == "KB4USR-3"
    g = next(x for x in c.get("/winlink/gateways").json()["gateways"]
             if x["call"] == "KB4USR-3")
    assert g["favorite"] and g["source"] == "user" and g["band"] == "2m"
    assert c.post("/winlink/gateways", json={"call": "X; reboot", "freq": 1,
                                             "mode": "packet"}).status_code == 422
    assert c.delete("/winlink/gateways/KB4USR-3").status_code == 200
    assert c.delete("/winlink/gateways/K4FAR-10").status_code == 404


def test_fault_and_clear_via_api(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    assert c.post("/radios/vu/fault/clear").status_code == 409  # not in fault
    assert c.post("/mock/vu/inject", json={"fault": "readback_bad"}).status_code == 200
    c.post("/radios/vu/lease", json={"profile": "vara_fm"})
    st = poll(c, state_is("fault"))
    assert "read-back mismatch" in st["fault"]["reason"]
    assert st["consumer"] is None
    poll(c, lambda s: s["state"] == "fault" and s["fault"]["latched"], timeout=5)
    c.post("/mock/vu/inject", json={"fault": "readback_ok"})
    r = c.post("/radios/vu/fault/clear")
    assert r.status_code == 200, r.json()
    poll(c, state_is("default"))


def test_mock_inject_validation(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    assert c.post("/mock/vu/inject", json={"fault": "explode"}).status_code == 422
    assert c.post("/mock/nope/inject", json={"fault": "knob"}).status_code == 404


def test_events_endpoint(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    evs = c.get("/events?limit=5").json()
    assert 0 < len(evs) <= 5
    assert all(e["level"] != "debug" for e in evs)
    assert {"ts", "kind", "message", "radio", "level", "data"} <= set(evs[0])
    assert all(e["radio"] == "vu" for e in c.get("/events?radio=vu").json())
    assert c.get("/events?limit=0").status_code == 422


def test_websocket_pushes_status_and_events(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    with c.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "status" and "vu" in first["data"]["radios"]
        hist = ws.receive_json()
        assert hist["type"] == "events" and hist["data"]
        c.post("/radios/vu/lease", json={"profile": "monitor"})
        seen_active_event = seen_active_status = False
        for _ in range(200):
            msg = ws.receive_json()
            if msg["type"] == "event" and msg["data"]["kind"] == "transition" \
                    and msg["data"]["data"]["to"] == "active":
                seen_active_event = True
            if msg["type"] == "status" and msg["data"]["radios"]["vu"]["state"] == "active":
                seen_active_status = True
            if seen_active_event and seen_active_status:
                break
        assert seen_active_event and seen_active_status


def test_web_ui_served(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "<title>RigBroker</title>" in r.text
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/static/style.css").status_code == 200
    assert c.get("/schedule").json() == []


def test_winlink_check_refused_when_read_only() -> None:
    broker = Broker.build(make_config(), mock=True, allow_tx=False, simulate=False)
    store = auth_store()
    with TestClient(create_app(broker, auth_store=store), headers={"X-RigBroker": "1"}) as c:
        assert c.post("/auth/login", json=LOGIN).status_code == 200
        poll(c, state_is("default"))
        for _ in range(3):  # repeated clicks must not bounce the radio
            r = c.post("/radios/vu/winlink/check")
            assert r.status_code == 409 and "read-only" in r.json()["reason"]
        st = c.get("/radios/vu").json()
        assert st["state"] == "default" and st["applied_profile"] == "aprs"
        assert not [e for e in broker.events.recent(0, kind="transition")
                    if e.data.get("to") == "switching" and e.data.get("frm") == "default"]


def test_inject_refused_outside_mock() -> None:
    cfg = make_config()
    broker = Broker.build(cfg, mock=True, allow_tx=False, simulate=False)
    broker.mock = False  # pretend hardware mode, without starting anything
    store = AuthStore(None)
    _, token = store.create_token("test")
    c = TestClient(create_app(broker, manage_lifecycle=False, auth_store=store),
                   headers={"Authorization": f"Bearer {token}"})
    assert c.post("/mock/vu/inject", json={"fault": "knob"}).status_code == 404


def test_set_power_via_api(client: tuple[TestClient, Broker]) -> None:
    c, _ = client
    r = c.post("/radios/vu/power", json={"profile": "aprs", "level": "L"})
    assert r.status_code == 200, r.text
    st = c.get("/radios/vu").json()
    assert st["profiles"]["aprs"]["power_level"] == "L"
    assert st["power_levels"] == {"L": 0.2, "M": 0.5}
    r = c.post("/radios/vu/power", json={"profile": "aprs", "level": "H"})
    assert r.status_code == 409 and "capped" in r.json()["reason"]
    c.post("/auth/logout")
    assert c.post("/radios/vu/power", json={"profile": "aprs", "level": "M"}).status_code in (
        401, 403)
