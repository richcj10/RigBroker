"""Mock-mode activity: makes a MockRig look alive (S-meter, busy channel, TX bursts).

Only used with ``--mock``. TX bursts happen only while a TX-capable mock consumer is
running and the channel is clear, like a real modem with carrier sense.
"""

from __future__ import annotations

import asyncio
import random
import time

from .config import RadioConfig
from .mock_rig import MockRig
from .services import MockServiceController

INJECTIONS = (
    "stuck_tx", "release_tx", "offline", "online", "readback_bad", "readback_ok",
    "busy", "clear", "knob",
)


class MockActivity:
    """Drives a MockRig's STRENGTH / DCD / PTT; supports manual fault injection."""

    def __init__(self, rig: MockRig, services: MockServiceController, cfg: RadioConfig,
                 rng: random.Random | None = None) -> None:
        self.rig = rig
        self.services = services
        self.cfg = cfg
        self.rng = rng or random.Random()
        self.hold_ptt = False  # injected stuck TX
        self.hold_dcd: bool | None = None  # injected busy channel

    def inject(self, what: str) -> str:
        """Apply a named fault/condition. Returns a description."""
        rig = self.rig
        if what == "stuck_tx":
            self.hold_ptt, rig.ptt = True, True
            return "PTT held on (stuck TX)"
        if what == "release_tx":
            self.hold_ptt, rig.ptt = False, False
            return "PTT released"
        if what == "offline":
            rig.offline = True
            return "rig offline (CI-V silent)"
        if what == "online":
            rig.offline = False
            return "rig online"
        if what == "readback_bad":
            rig.readback_override["RFPOWER"] = 0.9
            return "RFPOWER read-back will report 0.9"
        if what == "readback_ok":
            rig.readback_override.pop("RFPOWER", None)
            return "read-back normal"
        if what == "busy":
            self.hold_dcd, rig.dcd = True, True
            return "channel held busy"
        if what == "clear":
            self.hold_dcd, rig.dcd = None, False
            return "channel activity back to random"
        if what == "knob":
            rig.freq += 5000
            return f"VFO knob turned: {rig.freq} Hz"
        raise ValueError(f"unknown injection {what!r} (one of: {', '.join(INJECTIONS)})")

    async def run(self) -> None:
        rng, rig = self.rng, self.rig
        now = time.monotonic()
        busy_until, next_busy = 0.0, now + rng.uniform(8, 20)
        tx_until, next_tx = 0.0, now + rng.uniform(4, 10)
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            if not rig.power_on:
                continue
            tx_svcs = self.services.active & self.cfg.tx_services()
            transmitting = self.hold_ptt or now < tx_until
            if self.hold_dcd is None and now >= next_busy and not transmitting:
                busy_until, next_busy = now + rng.uniform(1, 4), now + rng.uniform(10, 30)
            busy = self.hold_dcd if self.hold_dcd is not None else now < busy_until
            if not self.hold_ptt:
                if tx_svcs and not busy and now >= next_tx:
                    if "direwolf@aprs" in tx_svcs:  # beacon / digipeat
                        tx_until, next_tx = now + 0.8, now + rng.uniform(15, 40)
                    else:  # packet / VARA session traffic
                        tx_until, next_tx = now + rng.uniform(0.5, 2.5), now + rng.uniform(2, 6)
                rig.ptt = bool(tx_svcs) and now < tx_until
            if rig.ptt:
                rig.dcd = False
                rig.levels["STRENGTH"] = 60.0
            else:
                rig.dcd = bool(busy)
                rig.levels["STRENGTH"] = (round(rng.uniform(-12, 25), 1) if busy
                                          else round(-54 + rng.uniform(0, 8), 1))
