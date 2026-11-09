from __future__ import annotations

import asyncio
import random

import pytest

from rigbroker.mock_rig import MockRig
from rigbroker.mock_sim import INJECTIONS, MockActivity
from rigbroker.services import MockServiceController
from tests.conftest import make_config


def sim() -> MockActivity:
    cfg = make_config().radios["vu"]
    return MockActivity(MockRig(), MockServiceController(), cfg, random.Random(1))


def test_injections() -> None:
    s = sim()
    for what in INJECTIONS:
        assert s.inject(what)
    s.inject("stuck_tx")
    assert s.rig.ptt and s.hold_ptt
    s.inject("release_tx")
    assert not s.rig.ptt
    s.inject("readback_bad")
    assert s.rig.readback_override == {"RFPOWER": 0.9}
    s.inject("readback_ok")
    assert s.rig.readback_override == {}
    with pytest.raises(ValueError):
        s.inject("explode")


async def test_no_tx_without_tx_consumer() -> None:
    s = sim()
    task = asyncio.create_task(s.run())
    for _ in range(10):
        await asyncio.sleep(0.05)
        assert not s.rig.ptt
    s.inject("busy")
    await asyncio.sleep(0.3)
    assert s.rig.dcd and s.rig.levels["STRENGTH"] > -20
    task.cancel()
