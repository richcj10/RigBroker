"""Keep Direwolf's APRS position in step with the radio's GPS - with precision removed.

* Every GPS fix is rounded to 0.1 arc-minute (~185 m) *before* it is stored or used, so the
  exact position never reaches a file, Direwolf or the air.
* The last rounded fix is saved; when Direwolf has no position yet (e.g. right after a
  restart, before the radio has a fix) that last known position is filled in automatically.
* When a new fix is more than ``threshold_km`` from Direwolf's position, nothing changes on
  its own: the web UI asks ("update Direwolf?") and the user accepts or dismisses.
* Beaconing stays the user's choice; this only keeps the position current.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .aprs import bearing_distance
from .direwolf import DirewolfManager
from .events import EventLog
from .gps import Position, to_grid

log = logging.getLogger("rigbroker.position")

STEP_DEG = 1 / 600  # 0.1 arc-minute (~185 m north-south)
THRESHOLD_KM = 0.3  # ignore GPS wander between neighbouring 0.1' cells

# apply(restart): write the Direwolf configs; restart a running Direwolf if ``restart``
ApplyFn = Callable[[bool], Awaitable[Any]]


def blur(lat: float, lon: float) -> tuple[float, float]:
    """Round to 0.1 arc-minute (the precision this station publishes)."""
    return (round(round(lat / STEP_DEG) * STEP_DEG, 6),
            round(round(lon / STEP_DEG) * STEP_DEG, 6))


class PositionSync:
    def __init__(self, direwolf: DirewolfManager, path: str | None, events: EventLog,
                 apply: ApplyFn, *, threshold_km: float = THRESHOLD_KM) -> None:
        self.direwolf = direwolf
        self.path = Path(os.path.expanduser(path)) if path else None
        self.events = events
        self.apply = apply
        self.threshold_km = threshold_km
        self.last: tuple[float, float] | None = None
        self.last_ts: float | None = None
        self.pending: tuple[float, float] | None = None
        self.pending_ts: float | None = None
        self.dismissed: tuple[float, float] | None = None
        self._load()

    # ------------------------------------------------------------ storage

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.last = blur(float(d["lat"]), float(d["lon"]))
            self.last_ts = float(d.get("ts", 0)) or None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("last GPS position %s unreadable (%s); ignoring it", self.path, exc)

    def _save(self) -> None:
        if self.path is None or self.last is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".gps-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"lat": self.last[0], "lon": self.last[1], "ts": self.last_ts}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ logic

    def direwolf_position(self) -> tuple[float, float] | None:
        s = self.direwolf.settings
        return None if s.lat is None or s.lon is None else (s.lat, s.lon)

    def _distance_km(self, a: tuple[float, float], b: tuple[float, float]) -> float:
        return bearing_distance(a[0], a[1], b[0], b[1])[0]

    async def _set_direwolf(self, pos: tuple[float, float], why: str, *, restart: bool) -> None:
        self.direwolf.update({"lat": pos[0], "lon": pos[1]})
        await self.apply(restart and self.direwolf.settings.beacon)
        self.events.emit("gps", f"Direwolf position set from {why} "
                                f"(grid {to_grid(pos[0], pos[1])}, rounded to 0.1')")

    async def startup(self) -> None:
        """Direwolf has no position yet: use the last known GPS position (if any)."""
        if self.direwolf_position() is None and self.last is not None:
            await self._set_direwolf(self.last, "the last known GPS position", restart=False)

    async def on_fix(self, fix: Position) -> None:
        """A GPS fix from the radio (exact) - only the rounded value is kept."""
        pos = blur(fix.lat, fix.lon)
        if pos != self.last:
            self.last, self.last_ts = pos, time.time()
            self._save()
        current = self.direwolf_position()
        if current is None:  # nothing to ask about: fill it in
            await self._set_direwolf(pos, "the radio's GPS", restart=True)
            return
        dist = self._distance_km(current, pos)
        if dist <= self.threshold_km:
            self.pending = self.pending_ts = None  # back where Direwolf thinks we are
            return
        if (self.dismissed is not None
                and self._distance_km(self.dismissed, pos) <= self.threshold_km):
            return  # the user already said no to (about) this position
        if self.pending is None or self._distance_km(self.pending, pos) > self.threshold_km:
            self.events.emit("gps", f"GPS position is {dist:.1f} km from Direwolf's - "
                                    "update Direwolf from the web page (APRS tab)")
        self.pending, self.pending_ts = pos, time.time()

    async def accept(self) -> bool:
        """Use the pending GPS position in Direwolf. False if nothing is pending."""
        if self.pending is None:
            return False
        pos, self.pending, self.pending_ts, self.dismissed = self.pending, None, None, None
        await self._set_direwolf(pos, "the radio's GPS (accepted in the web UI)", restart=True)
        return True

    def dismiss(self) -> bool:
        if self.pending is None:
            return False
        self.dismissed, self.pending, self.pending_ts = self.pending, None, None
        self.events.emit("gps", "kept Direwolf's position (GPS update dismissed)")
        return True

    def status(self) -> dict[str, Any]:
        """No coordinates here (status is public): distances and grid squares only."""
        cur = self.direwolf_position()
        out: dict[str, Any] = {
            "direwolf_set": cur is not None,
            "direwolf_grid": to_grid(cur[0], cur[1]) if cur else None,
            "last_ts": self.last_ts,
            "pending": None,
        }
        if self.pending is not None:
            out["pending"] = {
                "grid": to_grid(self.pending[0], self.pending[1]),
                "distance_km": round(self._distance_km(cur, self.pending), 2) if cur else None,
                "ts": self.pending_ts,
            }
        return out


__all__ = ["STEP_DEG", "THRESHOLD_KM", "PositionSync", "blur"]
