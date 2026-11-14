"""Shared fixtures: a fast-timing config and a harness around one RadioController."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from rigbroker.auth import AuthStore
from rigbroker.config import Config, parse_config
from rigbroker.events import EventLog
from rigbroker.jobs import MockJobRunner
from rigbroker.mock_rig import MockRig
from rigbroker.radio import RadioController
from rigbroker.rig_client import RigClient
from rigbroker.services import MockServiceController
from rigbroker.state import RadioState

TX_SERVICES = {"direwolf@aprs", "direwolf@pkt", "vara-fm"}

FAST_SAFETY: dict[str, Any] = {
    "tx_holdoff_s": 0.1,
    "clear_channel_timeout_s": 0.3,
    "clear_channel_hold_s": 0.02,
    "stuck_tx_s": 0.5,
    "poll_interval_s": 0.02,
    "poll_backoff_max_s": 0.05,
    "offline_after_failures": 2,
    "rig_timeout_s": 0.2,
    "power_on_timeout_s": 0.5,
    "recovery_attempts": 2,
    "recovery_backoff_s": 0.05,
    "max_auto_recoveries": 2,
    "fault_window_s": 60,
    "lease_grace_s": 0.05,
    "default_lease_minutes": 1,
}

BASE: dict[str, Any] = {
    "radios": {
        "vu": {
            "name": "ID-4100A",
            "rigctld": "127.0.0.1:4542",
            "rigctld_service": "rigctld-vu",
            "default_profile": "aprs",
            "power_levels": {"L": 0.2, "M": 0.5, "H": 0.9},
            "max_power_level": "M",
            "winlink_profile": "winlink_gw",
            "profiles": {
                "aprs": {"freq": 144390000, "mode": "FM", "power": 0.5, "squelch": 0.15,
                         "service": "direwolf@aprs", "rx_service": "direwolf@aprs-rx",
                         "preemptible": True, "transmits": True},
                "aprs_quiet": {"freq": 144390000, "mode": "FM", "squelch": 0.15,
                               "service": "direwolf@aprs-rx", "preemptible": True,
                               "max_minutes": 60},
                "winlink_pkt": {"freq": 145050000, "mode": "FM", "power": 0.5, "squelch": 0.15,
                                "service": "direwolf@pkt", "run": "pat connect ax25:///N0CALL-10",
                                "max_minutes": 10, "transmits": True},
                "vara_fm": {"freq": 145090000, "mode": "FM", "power": 0.5, "squelch": 0.15,
                            "service": "vara-fm", "max_minutes": 15, "transmits": True},
                "monitor": {"freq": 147240000, "mode": "FM", "squelch": 0.15, "volume": 0.2,
                            "service": "audio-stream", "max_minutes": 60},
                "winlink_gw": {"freq": 145050000, "mode": "FM", "power": 0.5, "squelch": 0.15,
                               "service": "direwolf@pkt", "run": "pat connect ax25:///{call}",
                               "winlink_mode": "packet", "max_minutes": 10, "transmits": True,
                               "freq_ranges": [[144000000, 148000000]]},
                # VARA FM remote session (Winlink Express on a PC): tuned to the chosen gateway
                "vara_gw": {"freq": 145090000, "mode": "FM", "power": 0.5, "squelch": 0.15,
                            "service": "vara-fm", "winlink_mode": "vara_fm", "max_minutes": 15,
                            "transmits": True,
                            "freq_ranges": [[144000000, 148000000], [420000000, 450000000]]},
                "off": {"rig_power": "off"},
            },
            "hooks": {"rig_power_cut": "relay-off", "usb_power_cycle": "uhubctl-cycle"},
        },
        "hf": {"name": "FT-891", "rigctld": "127.0.0.1:4543", "default_profile": "off",
               "profiles": {}},
    },
    "safety": FAST_SAFETY,
    "winlink": {
        "grid": "EM85ab",
        "state_file": None,  # memory only in tests
        "max_attempts": 3,
        "attempt_minutes": 1,
        "gateways": [
            {"call": "W4FAV-10", "freq": 145050000, "mode": "packet", "grid": "EM85cd",
             "favorite": True},
            {"call": "K4NEAR-10", "freq": 145070000, "mode": "packet", "grid": "EM85ab"},
            {"call": "K4FAR-10", "freq": 145010000, "mode": "packet", "grid": "EM75aa"},
            {"call": "W4HF", "freq": 7101000, "mode": "vara_hf", "grid": "EM85ab"},
            {"call": "N7VFM-2", "freq": 145570000, "mode": "vara_fm", "grid": "EM85ab"},
            {"call": "N7UHF-2", "freq": 441300000, "mode": "vara_fm", "grid": "EM85ab"},
        ],
    },
    "aprs": {"callsign": "KY4WL-5", "other_calls": ["KY4WL"], "radio": "vu",
             "msg_retry_s": 0.2, "msg_retries": 3},
    "direwolf": {"conf_dir": tempfile.mkdtemp(prefix="rb-direwolf-"), "settings_file": None},
}


def make_config(*, security: dict[str, Any] | None = None, **safety: Any) -> Config:
    data = copy.deepcopy(BASE)
    data["safety"].update(safety)
    data["security"] = {"auth_file": None, **(security or {})}
    return parse_config(data)


PASSWORD = "correct horse battery"
LOGIN = {"username": "admin", "password": PASSWORD}


def auth_store() -> AuthStore:
    """In-memory accounts: admin (PASSWORD, already set up)."""
    s = AuthStore(None)
    s.add_user("admin", PASSWORD, "admin", must_change=False)
    return s


async def wait_until(pred: Callable[[], bool], timeout: float = 3.0, what: str = "") -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what or pred}")
        await asyncio.sleep(0.005)


@dataclass
class Harness:
    rig: MockRig
    client: RigClient
    services: MockServiceController
    jobs: MockJobRunner
    events: EventLog
    radio: RadioController

    async def wait_state(self, state: RadioState, timeout: float = 3.0) -> None:
        await wait_until(lambda: self.radio.state is state, timeout,
                         f"state {state.value} (now {self.radio.state.value}); last events: "
                         + " | ".join(e.message for e in self.events.recent(8)))

    def transitions(self) -> list[tuple[str, str]]:
        return [(e.data["frm"], e.data["to"]) for e in self.events.recent(0, kind="transition")]

    def messages(self, kind: str) -> list[str]:
        return [e.message for e in self.events.recent(0, kind=kind)]

    def tx_active(self) -> set[str]:
        return self.services.active & TX_SERVICES

    def consumers(self) -> set[str]:
        """Active consumer services (excludes rigctld, which recovery restarts)."""
        return self.services.active - {"rigctld-vu"}


MakeHarness = Callable[..., Awaitable[Harness]]


@pytest.fixture
async def make() -> AsyncIterator[MakeHarness]:
    created: list[Harness] = []

    async def _make(*, allow_tx: bool = True, start: bool = True, rig: MockRig | None = None,
                    client: RigClient | None = None, jobs: MockJobRunner | None = None,
                    **safety: Any) -> Harness:
        cfg = make_config(**safety)
        mock = rig or MockRig()
        h = Harness(
            rig=mock,
            client=client or mock,
            services=MockServiceController(),
            jobs=jobs or MockJobRunner(),
            events=EventLog(maxlen=5000),
            radio=None,  # type: ignore[arg-type]
        )
        h.radio = RadioController("vu", cfg.radios["vu"], cfg.safety, h.client, h.services,
                                  h.jobs, h.events, allow_tx=allow_tx)
        created.append(h)
        if start:
            await h.radio.start()
        return h

    yield _make
    for h in created:
        await h.radio.stop()
