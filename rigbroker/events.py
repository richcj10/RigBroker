"""Event log: bounded in-memory history plus async subscribers, mirrored to logging."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Level = Literal["debug", "info", "warning", "error", "alert"]
_LOG_LEVEL = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "alert": logging.CRITICAL,
}
log = logging.getLogger("rigbroker.events")


@dataclass(frozen=True)
class Event:
    """One thing that happened."""

    kind: str
    message: str
    radio: str | None = None
    level: Level = "info"
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventLog:
    """Keeps the last ``maxlen`` events and fans them out to subscriber queues."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._subs: set[asyncio.Queue[Event]] = set()

    def emit(
        self, kind: str, message: str, *, radio: str | None = None, level: Level = "info",
        **data: Any,
    ) -> Event:
        """Record an event, log it, and push it to subscribers (dropping if a queue is full)."""
        ev = Event(kind=kind, message=message, radio=radio, level=level, data=data)
        self._events.append(ev)
        log.log(_LOG_LEVEL[level], "[%s] %s: %s", radio or "-", kind, message,
                extra={"event": ev.to_dict()})
        for q in list(self._subs):
            with contextlib.suppress(asyncio.QueueFull):  # slow subscriber: drop
                q.put_nowait(ev)
        return ev

    def recent(self, limit: int = 100, radio: str | None = None,
               kind: str | None = None) -> list[Event]:
        """Most recent events, oldest first."""
        evs = [e for e in self._events
               if (radio is None or e.radio == radio) and (kind is None or e.kind == kind)]
        return evs[-limit:] if limit > 0 else evs

    def subscribe(self, maxsize: int = 500) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subs.discard(q)
