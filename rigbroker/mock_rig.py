"""In-memory rig for tests and --mock mode, with fault injection."""

from __future__ import annotations

import asyncio
from typing import Any

from .rig_client import RigConnectionError, RigError, RigTimeout

_DEFAULT_PASSBAND = {"FM": 15000, "FMN": 10000, "AM": 6000, "USB": 2400, "LSB": 2400}


class MockRig:
    """Implements RigClient in memory.

    Fault injection (set attributes directly):
      - ``offline = True``: every call raises RigConnectionError
      - ``timeouts_remaining = n``: next n calls raise RigTimeout (after ``timeout_s``)
      - ``readback_override = {"freq": 1, "mode": "AM", "RFPOWER": 0.9}``: lie on reads
      - ``fail_commands = {"set_freq": -9}``: RPRT error for a command
      - ``ptt`` / ``dcd`` / ``levels["STRENGTH"]``: simulate TX, busy channel, signal
    """

    def __init__(self, *, freq: int = 145_000_000, mode: str = "FM", latency: float = 0.0) -> None:
        self.freq = freq
        self.mode = mode
        self.passband = _DEFAULT_PASSBAND.get(mode, 0)
        self.levels: dict[str, float] = {"RFPOWER": 1.0, "SQL": 0.0, "AF": 0.3, "STRENGTH": -54.0}
        self.ptt = False
        self.dcd = False
        self.power_on = True
        self.connected = False
        self.latency = latency
        self.offline = False
        self.timeouts_remaining = 0
        self.timeout_s = 0.01
        self.readback_override: dict[str, Any] = {}
        self.fail_commands: dict[str, int] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def writes(self) -> list[tuple[str, tuple[Any, ...]]]:
        """All state-changing calls so far (for asserting the broker never wrote)."""
        return [c for c in self.calls if c[0].startswith("set_")]

    async def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.offline:
            raise RigConnectionError(f"{name}: mock rig offline")
        if self.timeouts_remaining > 0:
            self.timeouts_remaining -= 1
            await asyncio.sleep(self.timeout_s)
            raise RigTimeout(f"{name}: mock timeout")
        if name in self.fail_commands:
            code = self.fail_commands[name]
            raise RigError(f"{name} failed: RPRT {code}", code)
        if not self.power_on and name not in ("set_powerstat", "get_powerstat", "connect", "close"):
            raise RigTimeout(f"{name}: rig powered off (no CI-V reply)")

    async def connect(self) -> None:
        await self._call("connect")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def get_freq(self) -> int:
        await self._call("get_freq")
        return int(self.readback_override.get("freq", self.freq))

    async def set_freq(self, hz: int) -> None:
        await self._call("set_freq", hz)
        self.freq = int(hz)

    async def get_mode(self) -> tuple[str, int]:
        await self._call("get_mode")
        return str(self.readback_override.get("mode", self.mode)), self.passband

    async def set_mode(self, mode: str, passband: int = 0) -> None:
        await self._call("set_mode", mode, passband)
        self.mode = mode.upper()
        self.passband = passband or _DEFAULT_PASSBAND.get(self.mode, 0)

    async def get_ptt(self) -> bool:
        await self._call("get_ptt")
        return bool(self.readback_override.get("ptt", self.ptt))

    async def get_dcd(self) -> bool:
        await self._call("get_dcd")
        return bool(self.readback_override.get("dcd", self.dcd))

    async def get_level(self, name: str) -> float:
        await self._call("get_level", name)
        key = name.upper()
        if key in self.readback_override:
            return float(self.readback_override[key])
        if key not in self.levels:
            raise RigError(f"get_level {key} failed: RPRT -1", -1)
        return self.levels[key]

    async def set_level(self, name: str, value: float) -> None:
        await self._call("set_level", name, value)
        self.levels[name.upper()] = float(value)

    async def get_powerstat(self) -> bool:
        await self._call("get_powerstat")
        return self.power_on

    async def set_powerstat(self, on: bool) -> None:
        await self._call("set_powerstat", on)
        self.power_on = on
        if not on:
            self.ptt = False

    async def get_info(self) -> str:
        await self._call("get_info")
        return "MockRig"
