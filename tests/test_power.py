"""TX power levels chosen in the web UI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rigbroker.config import ConfigError, parse_config
from rigbroker.power import PowerStore
from rigbroker.state import RadioState as S
from tests.conftest import BASE, MakeHarness


def test_store_persists(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "power.json"
    s = PowerStore(f)
    s.set("vu", "aprs", "M")
    assert PowerStore(f).get("vu", "aprs") == "M"
    s.set("vu", "aprs", None)
    assert PowerStore(f).get("vu", "aprs") is None
    f.write_text("not json")
    assert PowerStore(f).get("vu", "aprs") is None  # unreadable file: ignored


def test_config_validation() -> None:
    import copy

    d: dict[str, Any] = copy.deepcopy(BASE)
    d["radios"]["vu"]["max_power_level"] = "X"
    with pytest.raises(ConfigError, match="max_power_level"):
        parse_config(d)
    d["radios"]["vu"]["max_power_level"] = None
    d["radios"]["vu"]["power_levels"] = {"L": 1.5}
    with pytest.raises(ConfigError, match=r"0..1"):
        parse_config(d)
    ok = parse_config(copy.deepcopy(BASE)).radios["vu"]
    assert ok.allowed_power_levels() == {"L": 0.2, "M": 0.5}  # H is above the cap


async def test_set_level_on_live_profile(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    ok, msg = await h.radio.set_power_level("aprs", "L", by="web")
    assert ok, msg
    assert h.rig.levels["RFPOWER"] == 0.2
    st = h.radio.status()["profiles"]["aprs"]
    assert st["power_level"] == "L" and st["power"] == 0.2
    assert any("set by web" in m for m in h.messages("power"))


async def test_cap_and_validation(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    ok, msg = await h.radio.set_power_level("aprs", "H", by="web")
    assert not ok and "capped at M" in msg
    assert h.rig.levels["RFPOWER"] == 0.5  # unchanged
    assert not (await h.radio.set_power_level("nope", "L", by="web"))[0]
    assert not (await h.radio.set_power_level("monitor", "L", by="web"))[0]  # no power set


async def test_level_for_other_profile_used_on_next_tune(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    ok, msg = await h.radio.set_power_level("vara_fm", "L", by="web")
    assert ok and "next tune" in msg
    assert h.rig.levels["RFPOWER"] == 0.5  # aprs still on the rig, untouched
    assert (await h.radio.request_lease("vara_fm", holder="web")).ok
    await h.wait_state(S.ACTIVE)
    assert h.rig.levels["RFPOWER"] == 0.2  # applied and read back on the switch
    await h.radio.release()
    await h.wait_state(S.DEFAULT)
    assert h.rig.levels["RFPOWER"] == 0.5  # aprs keeps its own level


async def test_not_applied_while_transmitting(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    h.rig.ptt = True  # direwolf@aprs (a TX consumer) is keyed
    h.radio.safety = h.radio.safety.model_copy(update={"tx_holdoff_s": 0.05})

    task = asyncio.create_task(h.radio.set_power_level("aprs", "L", by="web"))
    await asyncio.sleep(0.15)
    assert h.rig.levels["RFPOWER"] == 0.5  # waited: not changed mid-transmission
    h.rig.ptt = False
    ok, _ = await task
    assert ok and h.rig.levels["RFPOWER"] == 0.2
