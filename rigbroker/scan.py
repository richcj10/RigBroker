"""Airband scan + hold.

Steps through the scan list; on each channel it watches the squelch (DCD) for ``scan_dwell_s``.
When the squelch opens it HOLDS: it stays until the squelch has been closed for
``scan_hang_s`` (so the reply is heard too) or the signal has lasted ``scan_max_hold_s``
(continuous ATIS / stuck carrier), then scans on. Skip leaves the channel out until the scan is
restarted. The telemetry poller reads DCD only every few seconds, so the scanner reads it
itself. Receive only: it only tunes inside the receive-only airband profile.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from .airband import AirbandChannels
from .config import AirbandConfig
from .events import EventLog
from .rig_client import RigError
from .state import RadioState

if TYPE_CHECKING:
    from .radio import RadioController

log = logging.getLogger("rigbroker.scan")

ScanState = Literal["off", "scanning", "holding"]


class Scanner:
    def __init__(self, radio: RadioController, channels: AirbandChannels, cfg: AirbandConfig,
                 events: EventLog) -> None:
        self.radio = radio
        self.channels = channels
        self.cfg = cfg
        self.events = events
        self.state: ScanState = "off"
        self.freq: int | None = None
        self.hold_since: float | None = None
        self.skipped: set[int] = set()
        self.stopped_reason = ""
        self._task: asyncio.Task[None] | None = None
        self._skip = asyncio.Event()

    # ------------------------------------------------------------ control

    def in_session(self) -> bool:
        r = self.radio
        return (r.state is RadioState.ACTIVE and r.applied == self.cfg.profile
                and r.manual_profile() is not None and r.fault is None)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, by: str) -> tuple[bool, str]:
        if not self.in_session():
            return False, "start listening first"
        if not self.channels.scan:
            return False, "the scan list is empty - tick some channels first"
        if self.running:
            return True, "already scanning"
        self.skipped.clear()
        self.stopped_reason = ""
        self.state = "scanning"  # right away, not only after the first step
        self._task = asyncio.create_task(self._run(), name="airband-scan")
        self.events.emit("scan", f"scan started by {by} ({len(self.channels.scan)} channels)",
                         radio=self.radio.id)
        return True, "scanning"

    async def stop(self, reason: str) -> bool:
        if not self.running:
            return False
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._finish(reason)
        return True

    def skip(self) -> bool:
        if self.state != "holding":
            return False
        self._skip.set()
        return True

    def _finish(self, reason: str) -> None:
        self.state, self.hold_since = "off", None
        self.stopped_reason = reason
        self.events.emit("scan", f"scan stopped: {reason}", radio=self.radio.id)

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "freq": self.freq if self.state != "off" else None,
                "name": self.channels.name_of(self.freq) if self.freq else "",
                "list": self.channels.scan, "skipped": sorted(self.skipped),
                "hold_s": round(time.monotonic() - self.hold_since, 1)
                if self.state == "holding" and self.hold_since else None,
                "stopped": self.stopped_reason}

    # ------------------------------------------------------------ loop

    async def _run(self) -> None:
        reason = "stopped"
        try:
            while True:
                todo = [f for f in self.channels.scan if f not in self.skipped]
                if not todo:
                    reason = "every channel was skipped" if self.skipped else "scan list is empty"
                    return
                for f in todo:
                    if not self.in_session():
                        reason = "the airband session ended"
                        return
                    if f in self.skipped or f not in self.channels.scan:
                        continue
                    self.state, self.freq = "scanning", f
                    ok, msg = await self.radio.scan_tune(f)
                    if not ok:
                        log.info("scan: %s %s", f, msg)
                        await asyncio.sleep(0.2)
                        continue
                    if await self._signal():
                        await self._hold(f)
        except RigError as exc:
            reason = f"rig error: {exc}"
        finally:
            task = asyncio.current_task()
            if task is None or not task.cancelling():  # stop() reports its own reason
                self._finish(reason)

    async def _dcd(self) -> bool:
        open_ = await self.radio.rig.get_dcd()
        self.radio.telemetry.dcd = open_  # keep the signal light live while scanning
        return open_

    async def _signal(self) -> bool:
        """Watch the squelch for the dwell time (after a short settle)."""
        end = time.monotonic() + self.cfg.scan_dwell_s
        await asyncio.sleep(min(0.25, self.cfg.scan_dwell_s / 2))
        while True:
            if await self._dcd():
                return True
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.1)

    async def _hold(self, f: int) -> None:
        now = time.monotonic()
        self.state, self.hold_since, last_open = "holding", now, now
        self._skip.clear()
        name = self.channels.name_of(f)
        self.events.emit("scan", f"holding on {f / 1e6:.3f}{' ' + name if name else ''}",
                         radio=self.radio.id)
        while self.in_session():
            now = time.monotonic()
            if self._skip.is_set():
                self.skipped.add(f)
                self.events.emit("scan", f"skipped {f / 1e6:.3f} until the scan restarts",
                                 radio=self.radio.id)
                return
            if now - self.hold_since > self.cfg.scan_max_hold_s:
                self.events.emit("scan", f"{f / 1e6:.3f} busy for "
                                         f"{self.cfg.scan_max_hold_s:g} s - scanning on",
                                 radio=self.radio.id)
                return
            if await self._dcd():
                last_open = now
            elif now - last_open >= self.cfg.scan_hang_s:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._skip.wait(), 0.2)


__all__ = ["Scanner"]
