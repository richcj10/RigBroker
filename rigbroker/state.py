"""Per-radio state machine: a pure, table-driven transition function.

``transition(state, event, ctx)`` returns the new state and the actions the controller must
perform, or ``None`` if the event is not valid in that state (or its guard fails). No I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class RadioState(StrEnum):
    BOOT = "boot"
    DEFAULT = "default"
    SWITCHING = "switching"
    ACTIVE = "active"
    RETURNING = "returning"
    FAULT = "fault"


class Ev(StrEnum):
    START = "start"
    LEASE_GRANTED = "lease_granted"
    STEPS_OK = "steps_ok"
    CLEAR_CHANNEL_TIMEOUT = "clear_channel_timeout"
    STEP_FAILED = "step_failed"
    RELEASE = "release"
    JOB_DONE = "job_done"
    JOB_FAILED = "job_failed"
    LEASE_EXPIRED = "lease_expired"
    STUCK_TX = "stuck_tx"
    UNEXPECTED_TX = "unexpected_tx"  # PTT while no TX-capable consumer runs
    RIG_OFFLINE = "rig_offline"
    RIG_DRIFT = "rig_drift"
    RECOVERY_OK = "recovery_ok"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    FAULT_CLEAR = "fault_clear"


class Act(StrEnum):
    SWITCH_DEFAULT = "switch_default"  # begin switching to the default profile
    SWITCH_TARGET = "switch_target"  # install the pending lease and switch to its profile
    START_LEASE = "start_lease"  # lease timer + run job (entering ACTIVE)
    NEXT_CANDIDATE = "next_candidate"  # same lease, next plan step (e.g. next gateway)
    END_LEASE = "end_lease"
    DEQUEUE = "dequeue"
    TX_INHIBIT = "tx_inhibit"  # cancel switch/job, stop all TX-capable consumers
    RECOVER = "recover"  # bounded automatic recovery
    ALERT = "alert"
    CLEAR_QUEUE = "clear_queue"
    LATCH = "latch"  # fault stays until manual clear
    RESET_FAULTS = "reset_faults"


@dataclass(frozen=True)
class Ctx:
    """Facts guards may depend on (computed by the controller)."""

    target_is_default: bool = False
    tx_consumer_running: bool = False
    rig_ok: bool = False  # online and in RX
    more_candidates: bool = False  # lease plan has another step and time left for it


@dataclass(frozen=True)
class Transition:
    to: RadioState
    actions: tuple[Act, ...]


Guard = Callable[[Ctx], bool]


def _always(_: Ctx) -> bool:
    return True


@dataclass(frozen=True)
class _Rule:
    to: RadioState
    actions: tuple[Act, ...]
    guard: Guard = _always
    guard_name: str = ""


S, E, A = RadioState, Ev, Act

_FAULT_ENTRY = (A.END_LEASE, A.CLEAR_QUEUE, A.TX_INHIBIT, A.ALERT, A.RECOVER)
_FAULT_AGAIN = (A.TX_INHIBIT, A.ALERT)
_LIVE = (S.DEFAULT, S.SWITCHING, S.ACTIVE, S.RETURNING)


def _build() -> dict[tuple[RadioState, Ev], list[_Rule]]:
    t: dict[tuple[RadioState, Ev], list[_Rule]] = {}

    def add(
        frm: RadioState, ev: Ev, to: RadioState, acts: tuple[Act, ...],
        guard: Guard = _always, name: str = "",
    ) -> None:
        t.setdefault((frm, ev), []).append(_Rule(to, acts, guard, name))

    add(S.BOOT, E.START, S.SWITCHING, (A.SWITCH_DEFAULT,))
    add(S.DEFAULT, E.LEASE_GRANTED, S.SWITCHING, (A.SWITCH_TARGET,))
    add(S.ACTIVE, E.LEASE_GRANTED, S.SWITCHING, (A.END_LEASE, A.SWITCH_TARGET))  # preempt

    add(S.SWITCHING, E.STEPS_OK, S.DEFAULT, (A.DEQUEUE,),
        lambda c: c.target_is_default, "target is default")
    add(S.SWITCHING, E.STEPS_OK, S.ACTIVE, (A.START_LEASE,),
        lambda c: not c.target_is_default, "target is not default")
    # Busy gateway channel: try the next gateway in the plan, else give up the lease.
    add(S.SWITCHING, E.CLEAR_CHANNEL_TIMEOUT, S.SWITCHING, (A.NEXT_CANDIDATE,),
        lambda c: not c.target_is_default and c.more_candidates, "more candidates")
    add(S.SWITCHING, E.CLEAR_CHANNEL_TIMEOUT, S.RETURNING, (A.END_LEASE, A.SWITCH_DEFAULT),
        lambda c: not c.target_is_default, "target is not default")
    add(S.SWITCHING, E.RELEASE, S.RETURNING, (A.END_LEASE, A.SWITCH_DEFAULT),
        lambda c: not c.target_is_default, "target is not default")
    add(S.SWITCHING, E.STEP_FAILED, S.FAULT, _FAULT_ENTRY)

    for ev in (E.RELEASE, E.JOB_DONE, E.LEASE_EXPIRED):
        add(S.ACTIVE, ev, S.RETURNING, (A.END_LEASE, A.SWITCH_DEFAULT))
    # Failed attempt (e.g. gateway didn't answer): retune to the next gateway, same lease.
    add(S.ACTIVE, E.JOB_FAILED, S.SWITCHING, (A.NEXT_CANDIDATE,),
        lambda c: c.more_candidates, "more candidates")
    add(S.ACTIVE, E.JOB_FAILED, S.RETURNING, (A.END_LEASE, A.SWITCH_DEFAULT))

    add(S.RETURNING, E.STEPS_OK, S.DEFAULT, (A.DEQUEUE,))
    add(S.RETURNING, E.STEP_FAILED, S.FAULT, _FAULT_ENTRY)

    for st in _LIVE:
        add(st, E.STUCK_TX, S.FAULT, _FAULT_ENTRY)
        add(st, E.UNEXPECTED_TX, S.FAULT, _FAULT_ENTRY)
        add(st, E.RIG_DRIFT, S.FAULT, _FAULT_ENTRY)
        add(st, E.RIG_OFFLINE, S.FAULT, _FAULT_ENTRY,
            lambda c: c.tx_consumer_running, "TX consumer running")
    add(S.FAULT, E.STUCK_TX, S.FAULT, _FAULT_AGAIN)
    add(S.FAULT, E.UNEXPECTED_TX, S.FAULT, _FAULT_AGAIN)
    add(S.FAULT, E.RIG_DRIFT, S.FAULT, _FAULT_AGAIN)

    add(S.FAULT, E.RECOVERY_OK, S.RETURNING, (A.SWITCH_DEFAULT,),
        lambda c: c.rig_ok, "rig online and RX")
    add(S.FAULT, E.RECOVERY_EXHAUSTED, S.FAULT, (A.LATCH, A.ALERT))
    add(S.FAULT, E.FAULT_CLEAR, S.RETURNING, (A.RESET_FAULTS, A.SWITCH_DEFAULT),
        lambda c: c.rig_ok, "rig online and RX")
    return t


TABLE = _build()


def transition(state: RadioState, event: Ev, ctx: Ctx | None = None) -> Transition | None:
    """Return the transition for (state, event) whose guard holds, else None."""
    c = ctx or Ctx()
    for rule in TABLE.get((state, event), ()):
        if rule.guard(c):
            return Transition(rule.to, rule.actions)
    return None


def table_rows() -> list[tuple[str, str, str, str, str]]:
    """(from, event, guard, to, actions) rows — for docs and the UI."""
    rows = []
    for (frm, ev), rules in TABLE.items():
        for r in rules:
            rows.append((frm.value, ev.value, r.guard_name or "—", r.to.value,
                         ", ".join(a.value for a in r.actions)))
    return rows
