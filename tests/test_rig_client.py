from __future__ import annotations

import socket
from collections.abc import AsyncIterator

import pytest

from rigbroker.rig_client import RigConnectionError, RigctldClient, RigError, RigTimeout
from tests.fake_rigctld import FakeRigctld


@pytest.fixture
async def fake() -> AsyncIterator[FakeRigctld]:
    srv = FakeRigctld()
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
async def client(fake: FakeRigctld) -> AsyncIterator[RigctldClient]:
    c = RigctldClient("127.0.0.1", fake.port, timeout=0.3)
    yield c
    await c.close()


async def test_roundtrip(client: RigctldClient, fake: FakeRigctld) -> None:
    await client.set_freq(144_390_000)
    assert await client.get_freq() == 144_390_000
    await client.set_mode("fm")
    assert await client.get_mode() == ("FM", 15000)
    await client.set_level("RFPOWER", 0.5)
    assert await client.get_level("RFPOWER") == 0.5
    assert await client.get_ptt() is False
    fake.rig.ptt = True
    assert await client.get_ptt() is True
    fake.rig.dcd = True
    assert await client.get_dcd() is True
    await client.set_powerstat(True)
    assert await client.get_powerstat() is True
    assert await client.get_info() == "FakeRigctld"
    assert fake.connections == 1


async def test_rprt_error_keeps_stream_in_sync(client: RigctldClient, fake: FakeRigctld) -> None:
    fake.rig.fail_commands["set_freq"] = -9
    with pytest.raises(RigError) as ei:
        await client.set_freq(1)
    assert ei.value.code == -9
    with pytest.raises(RigError) as ei:
        await client.get_level("BOGUS")
    assert ei.value.code == -1
    assert await client.get_freq() == 145_000_000
    assert fake.connections == 1


async def test_timeout_then_reconnect(client: RigctldClient, fake: FakeRigctld) -> None:
    assert await client.get_freq()
    fake.silent = True
    with pytest.raises(RigTimeout):
        await client.get_freq()
    fake.silent = False
    assert await client.get_freq() == 145_000_000
    assert fake.connections == 2


async def test_dropped_connection_then_reconnect(client: RigctldClient,
                                                 fake: FakeRigctld) -> None:
    assert await client.get_freq()
    fake.drop_next = True
    with pytest.raises(RigConnectionError):
        await client.get_freq()
    assert await client.get_freq() == 145_000_000


async def test_connection_refused() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = RigctldClient("127.0.0.1", port, timeout=0.3)
    with pytest.raises(RigConnectionError):
        await c.get_freq()


async def test_plain_protocol_of_fake(fake: FakeRigctld) -> None:
    # The fake also answers non-extended commands like real rigctld.
    assert await fake.dispatch("f") == "145000000\n"
    assert await fake.dispatch("F 144390000") == "RPRT 0\n"
    assert await fake.dispatch("m") == "FM\n15000\n"
