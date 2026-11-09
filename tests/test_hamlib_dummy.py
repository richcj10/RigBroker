"""End to end against REAL Hamlib: rigctld -m 1 (dummy rig). Skipped if rigctld is missing."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

from rigbroker.rig_client import RigctldClient
from rigbroker.state import RadioState as S
from tests.conftest import MakeHarness

pytestmark = pytest.mark.skipif(shutil.which("rigctld") is None, reason="needs Hamlib rigctld")


@pytest.fixture
def dummy_rigctld() -> Iterator[int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(["rigctld", "-m", "1", "-T", "127.0.0.1", "-t", str(port),
                             "-P", "RIG"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        yield port
    finally:
        proc.terminate()  # may linger while a client is still connected
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(3)


async def test_client_against_real_hamlib(dummy_rigctld: int) -> None:
    c = RigctldClient("127.0.0.1", dummy_rigctld, timeout=2)
    await c.set_freq(144_390_000)
    assert await c.get_freq() == 144_390_000
    await c.set_mode("FM")
    assert (await c.get_mode())[0] == "FM"
    await c.set_level("RFPOWER", 0.5)
    assert abs(await c.get_level("RFPOWER") - 0.5) < 0.02
    assert await c.get_ptt() is False
    assert await c.get_dcd() in (False, True)
    await c.close()


async def test_broker_switches_real_hamlib(make: MakeHarness, dummy_rigctld: int) -> None:
    client = RigctldClient("127.0.0.1", dummy_rigctld, timeout=2)
    h = await make(client=client)
    await h.wait_state(S.DEFAULT, timeout=6)
    check = RigctldClient("127.0.0.1", dummy_rigctld, timeout=2)
    assert await check.get_freq() == 144_390_000
    assert (await h.radio.request_lease("monitor", holder="t")).ok
    await h.wait_state(S.ACTIVE, timeout=6)
    assert await check.get_freq() == 147_240_000
    await h.radio.release()
    await h.wait_state(S.DEFAULT, timeout=6)
    assert await check.get_freq() == 144_390_000
    await check.close()
