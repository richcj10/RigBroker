"""Telemetry poller: reads each rig periodically, detects offline/online and stuck TX."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .config import SafetyConfig
from .rig_client import RigClient, RigConnectionError, RigError, RigTimeout

log = logging.getLogger("rigbroker.telemetry")


@dataclass
class Telemetry:
    """Latest known rig readings."""

    online: bool = False
    polling: bool = True  # False while the active profile has the rig powered off
    freq: int | None = None
    mode: str | None = None
    ptt: bool | None = None
    dcd: bool | None = None
    strength: float | None = None
    failures: int = 0
    last_ok: float | None = None  # wall clock
    last_error: str | None = None
    # which link failed while offline: digirig_missing | rigctld_down | radio_not_answering
    # | rig_error (see hwcheck.py); None while online
    failure: str | None = None
    # interface presence from the last check: {"serial": bool|None, "audio": bool|None}
    interface: dict[str, bool | None] | None = None
    tx_since: float | None = None  # monotonic

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tx_for_s"] = round(time.monotonic() - self.tx_since, 1) if self.tx_since else 0.0
        del d["tx_since"]
        return d


@dataclass(frozen=True)
class Sample:
    """One successful poll."""

    started_mono: float
    freq: int
    mode: str
    ptt: bool
    dcd: bool
    strength: float | None
    fresh: bool = True  # False on a light poll: freq/mode/dcd/strength are the last readings


class PollerCallbacks(Protocol):
    """Implemented by the RadioController."""

    def should_poll(self) -> bool: ...
    async def on_sample(self, sample: Sample) -> None: ...
    async def on_offline(self, reason: str) -> None: ...
    def diagnose(self, exc: RigError) -> tuple[str, str]: ...
    async def on_failure_changed(self, code: str, message: str) -> None: ...
    async def on_online(self) -> None: ...
    async def on_stuck_tx(self, seconds: float) -> None: ...


class TelemetryPoller:
    """Polls freq, mode, PTT, DCD and STRENGTH every ``poll_interval_s``.

    After ``offline_after_failures`` consecutive failures the rig is marked offline and the
    poll interval backs off exponentially up to ``poll_backoff_max_s``.
    """

    def __init__(self, rig: RigClient, safety: SafetyConfig, tel: Telemetry,
                 cb: PollerCallbacks) -> None:
        self.rig = rig
        self.safety = safety
        self.tel = tel
        self.cb = cb
        self._offline_announced = False
        self._stuck_reported = False
        self._cycle = 0

    async def run(self) -> None:
        """Poll forever (cancel to stop). A bug in one poll never kills the poller."""
        while True:
            try:
                delay = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telemetry poll crashed; continuing")
                delay = self.safety.poll_interval_s
            await asyncio.sleep(delay)

    async def poll_once(self) -> float:
        """Do one poll; return the delay before the next one."""
        s, t = self.safety, self.tel
        if not self.cb.should_poll():
            t.polling, t.online, t.ptt, t.tx_since = False, False, False, None
            self._stuck_reported = False
            return s.poll_interval_s
        t.polling = True
        started = time.monotonic()
        # PTT every poll (stuck/unexpected-TX watchdogs); the rest only every Nth poll, so
        # rigctld's single command queue stays free for a modem's PTT on/off (a PTT
        # command stuck behind CI-V reads keys the radio late and clips the packet)
        full = (self._cycle % s.full_poll_every == 0 or not t.online or t.freq is None
                or t.mode is None)
        self._cycle += 1
        try:
            if full:
                freq = await self.rig.get_freq()
                mode, _ = await self.rig.get_mode()
            ptt = await self.rig.get_ptt()
            strength: float | None
            if full:
                dcd = await self.rig.get_dcd()
                try:
                    strength = await self.rig.get_level("STRENGTH")
                except (RigTimeout, RigConnectionError):
                    raise
                except RigError:
                    strength = None  # level not supported by this rig
        except RigError as exc:
            t.failures += 1
            code, message = self.cb.diagnose(exc)
            t.last_error = message
            th = s.offline_after_failures
            if t.failures >= th:
                if not self._offline_announced:
                    self._offline_announced = True
                    t.online, t.failure = False, code
                    await self.cb.on_offline(message)
                elif code != t.failure:  # e.g. USB back, but rigctld still down
                    t.failure = code
                    await self.cb.on_failure_changed(code, message)
                # cap the exponent: failures grows forever while the rig is gone, and
                # 2 ** ~1030 no longer fits in a float
                steps = min(t.failures - th + 1, 30)
                return float(min(s.poll_interval_s * 2 ** steps, s.poll_backoff_max_s))
            return s.poll_interval_s

        now = time.monotonic()
        was_online = t.online
        if full:
            t.freq, t.mode, t.dcd, t.strength = freq, mode, dcd, strength
        else:  # light poll: reuse the last full readings
            assert t.freq is not None and t.mode is not None
            freq, mode, dcd, strength = t.freq, t.mode, bool(t.dcd), t.strength
        t.ptt = ptt
        t.failures, t.online, t.last_ok, t.last_error = 0, True, time.time(), None
        t.failure = None
        self._offline_announced = False
        if ptt:
            if t.tx_since is None:
                t.tx_since = now
            dur = now - t.tx_since
            if dur > s.stuck_tx_s and not self._stuck_reported:
                self._stuck_reported = True
                await self.cb.on_stuck_tx(dur)
        else:
            t.tx_since = None
            self._stuck_reported = False
        if not was_online:
            await self.cb.on_online()
        await self.cb.on_sample(Sample(started, freq, mode, ptt, dcd, strength, fresh=full))
        return s.poll_interval_s
