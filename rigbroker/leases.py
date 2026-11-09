"""Leases, priorities and the (pure) arbitration rules."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from .state import RadioState


class Priority(IntEnum):
    """manual > scheduled > default."""

    DEFAULT = 0
    SCHEDULED = 1
    MANUAL = 2

    @classmethod
    def parse(cls, value: str | int | Priority) -> Priority:
        if isinstance(value, Priority):
            return value
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[value.upper()]
        except KeyError:
            raise ValueError(f"unknown priority {value!r} (manual|scheduled|default)") from None


@dataclass(frozen=True)
class PlanStep:
    """One attempt inside a lease: tune to ``freq`` and run ``run`` (e.g. one gateway)."""

    label: str  # shown to humans, e.g. "W4ABC-10"
    freq: int  # Hz; must lie inside the profile's freq_ranges
    run: str  # command for this attempt
    key: str  # identity for history (gateway callsign)
    timeout_s: float  # per-attempt limit (also bounded by the lease's remaining time)

    def to_dict(self) -> dict[str, object]:
        return {"label": self.label, "freq": self.freq, "key": self.key}


@dataclass
class Lease:
    """A holder's claim on a radio profile for a bounded time."""

    profile: str
    holder: str
    priority: Priority
    duration_s: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    created_mono: float = field(default_factory=time.monotonic)
    started_mono: float | None = None
    plan: list[PlanStep] = field(default_factory=list)
    attempt: int = 0  # index into plan

    def current_step(self) -> PlanStep | None:
        return self.plan[self.attempt] if self.plan else None

    def remaining_s(self, now: float | None = None) -> float:
        """Seconds left; counts from grant (switching time is included)."""
        start = self.started_mono if self.started_mono is not None else time.monotonic()
        now = time.monotonic() if now is None else now
        return max(0.0, self.duration_s - (now - start))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "profile": self.profile,
            "holder": self.holder,
            "priority": self.priority.name.lower(),
            "duration_s": round(self.duration_s, 1),
            "remaining_s": round(self.remaining_s(), 1) if self.started_mono else None,
            "created": self.created,
            "plan": [s.to_dict() for s in self.plan],
            "attempt": self.attempt,
        }


class Verdict(StrEnum):
    GRANT = "granted"
    PREEMPT = "preempted"
    QUEUE = "queued"
    REJECT = "rejected"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


@dataclass(frozen=True)
class Holding:
    """What currently holds the radio, as seen by arbitration."""

    state: RadioState
    profile: str | None
    holder: str | None
    priority: Priority
    preemptible: bool


def arbitrate(h: Holding, req: Lease, *, queue: bool, rig_available: bool) -> Decision:
    """Decide what happens to a lease request. Pure function.

    Rules:
      * FAULT / BOOT / rig offline -> reject.
      * SWITCHING / RETURNING -> queue if asked, else reject.
      * DEFAULT: the default profile is always preemptible -> grant (any priority above default).
      * ACTIVE: higher priority preempts a preemptible profile; a holder may replace its own
        lease at the same or higher priority; otherwise queue if asked, else reject.
    """
    if h.state is RadioState.FAULT:
        return Decision(Verdict.REJECT, "radio is in FAULT (TX inhibited); clear the fault first")
    if h.state is RadioState.BOOT:
        return Decision(Verdict.REJECT, "waiting for the rig to answer (off or not connected?)")
    if not rig_available:
        return Decision(Verdict.REJECT, "rig is offline")
    if req.priority <= Priority.DEFAULT:
        return Decision(Verdict.REJECT, "lease priority must be scheduled or manual")
    if h.state in (RadioState.SWITCHING, RadioState.RETURNING):
        if queue:
            return Decision(Verdict.QUEUE, f"radio is {h.state.value}; queued")
        return Decision(Verdict.REJECT, f"radio is {h.state.value} (to {h.profile})")
    if h.state is RadioState.DEFAULT:
        return Decision(Verdict.GRANT, "radio idle on default profile")
    # ACTIVE
    if req.holder == h.holder and req.priority >= h.priority:
        return Decision(Verdict.PREEMPT, f"{req.holder} replaces its own lease on {h.profile}")
    if req.priority > h.priority and h.preemptible:
        return Decision(
            Verdict.PREEMPT,
            f"{req.priority.name.lower()} preempts {h.priority.name.lower()} lease on {h.profile}",
        )
    why = (
        f"profile '{h.profile}' is not preemptible"
        if not h.preemptible
        else f"priority {req.priority.name.lower()} is not higher than {h.priority.name.lower()}"
    )
    if queue:
        return Decision(Verdict.QUEUE, f"radio busy ({why}); queued")
    return Decision(Verdict.REJECT, f"radio busy: '{h.profile}' held by {h.holder}; {why}")
