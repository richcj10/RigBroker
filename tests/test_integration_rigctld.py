"""End to end: RadioController -> RigctldClient (TCP) -> FakeRigctld -> MockRig."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from rigbroker.rig_client import RigctldClient
from rigbroker.state import RadioState as S
from tests.conftest import MakeHarness
from tests.fake_rigctld import FakeRigctld


@pytest.fixture
async def fake() -> AsyncIterator[FakeRigctld]:
    srv = FakeRigctld()
    await srv.start()
    yield srv
    await srv.stop()


async def test_switch_over_tcp(make: MakeHarness, fake: FakeRigctld) -> None:
    client = RigctldClient("127.0.0.1", fake.port, timeout=0.3)
    h = await make(rig=fake.rig, client=client)
    await h.wait_state(S.DEFAULT)
    assert fake.rig.freq == 144_390_000 and fake.rig.levels["RFPOWER"] == 0.5
    assert (await h.radio.request_lease("monitor", holder="web")).ok
    await h.wait_state(S.ACTIVE)
    assert fake.rig.freq == 147_240_000
    await h.radio.release()
    await h.wait_state(S.DEFAULT)


async def test_readback_mismatch_over_tcp(make: MakeHarness, fake: FakeRigctld) -> None:
    client = RigctldClient("127.0.0.1", fake.port, timeout=0.3)
    h = await make(rig=fake.rig, client=client, max_auto_recoveries=0)
    await h.wait_state(S.DEFAULT)
    fake.rig.readback_override = {"mode": "AM"}
    assert (await h.radio.request_lease("vara_fm", holder="web")).ok
    await h.wait_state(S.FAULT)
    assert h.radio.fault and "mode AM != FM" in h.radio.fault.reason
    assert not h.tx_active()


async def test_rigctld_goes_silent_then_returns(make: MakeHarness, fake: FakeRigctld) -> None:
    client = RigctldClient("127.0.0.1", fake.port, timeout=0.1)
    h = await make(rig=fake.rig, client=client, recovery_backoff_s=0.3)
    await h.wait_state(S.DEFAULT)
    fake.silent = True
    await h.wait_state(S.FAULT, timeout=3)  # TX consumer running -> fault
    fake.silent = False
    await h.wait_state(S.DEFAULT, timeout=4)
