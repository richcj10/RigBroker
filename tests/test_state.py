from __future__ import annotations

import pytest

from rigbroker.state import TABLE, Act, Ctx, Ev, RadioState, table_rows, transition

S = RadioState
LIVE = (S.DEFAULT, S.SWITCHING, S.ACTIVE, S.RETURNING)


def test_boot_switches_to_default() -> None:
    tr = transition(S.BOOT, Ev.START)
    assert tr and tr.to is S.SWITCHING and tr.actions == (Act.SWITCH_DEFAULT,)


def test_switch_completion_depends_on_target() -> None:
    to_default = transition(S.SWITCHING, Ev.STEPS_OK, Ctx(target_is_default=True))
    to_active = transition(S.SWITCHING, Ev.STEPS_OK, Ctx(target_is_default=False))
    assert to_default and to_default.to is S.DEFAULT and Act.DEQUEUE in to_default.actions
    assert to_active and to_active.to is S.ACTIVE and Act.START_LEASE in to_active.actions


@pytest.mark.parametrize("ev", [Ev.RELEASE, Ev.JOB_DONE, Ev.LEASE_EXPIRED])
def test_active_lease_end_returns(ev: Ev) -> None:
    tr = transition(S.ACTIVE, ev)
    assert tr and tr.to is S.RETURNING
    assert tr.actions == (Act.END_LEASE, Act.SWITCH_DEFAULT)


def test_preempt_ends_old_lease_first() -> None:
    tr = transition(S.ACTIVE, Ev.LEASE_GRANTED)
    assert tr and tr.actions == (Act.END_LEASE, Act.SWITCH_TARGET)


def test_clear_channel_timeout_is_not_a_fault() -> None:
    tr = transition(S.SWITCHING, Ev.CLEAR_CHANNEL_TIMEOUT, Ctx(target_is_default=False))
    assert tr and tr.to is S.RETURNING
    # returning to default never "times out" into anything
    assert transition(S.SWITCHING, Ev.CLEAR_CHANNEL_TIMEOUT, Ctx(target_is_default=True)) is None


def test_failed_attempt_tries_next_candidate_or_returns() -> None:
    nxt = transition(S.ACTIVE, Ev.JOB_FAILED, Ctx(more_candidates=True))
    assert nxt and nxt.to is S.SWITCHING and nxt.actions == (Act.NEXT_CANDIDATE,)
    done = transition(S.ACTIVE, Ev.JOB_FAILED, Ctx(more_candidates=False))
    assert done and done.to is S.RETURNING and Act.END_LEASE in done.actions


def test_busy_channel_tries_next_candidate() -> None:
    nxt = transition(S.SWITCHING, Ev.CLEAR_CHANNEL_TIMEOUT, Ctx(more_candidates=True))
    assert nxt and nxt.to is S.SWITCHING and nxt.actions == (Act.NEXT_CANDIDATE,)
    # never for the default profile
    assert transition(S.SWITCHING, Ev.CLEAR_CHANNEL_TIMEOUT,
                      Ctx(target_is_default=True, more_candidates=True)) is None


@pytest.mark.parametrize("st", LIVE)
@pytest.mark.parametrize("ev", [Ev.STUCK_TX, Ev.UNEXPECTED_TX, Ev.RIG_DRIFT])
def test_safety_events_fault_from_every_live_state(st: RadioState, ev: Ev) -> None:
    tr = transition(st, ev)
    assert tr and tr.to is S.FAULT
    assert Act.TX_INHIBIT in tr.actions and Act.ALERT in tr.actions


@pytest.mark.parametrize("st", LIVE)
def test_rig_offline_faults_only_with_tx_consumer(st: RadioState) -> None:
    assert transition(st, Ev.RIG_OFFLINE, Ctx(tx_consumer_running=False)) is None
    tr = transition(st, Ev.RIG_OFFLINE, Ctx(tx_consumer_running=True))
    assert tr and tr.to is S.FAULT


def test_every_fault_entry_inhibits_tx_and_ends_lease() -> None:
    for (frm, _), rules in TABLE.items():
        for r in rules:
            if r.to is S.FAULT and frm is not S.FAULT:
                assert Act.TX_INHIBIT in r.actions
                assert Act.END_LEASE in r.actions
                assert Act.CLEAR_QUEUE in r.actions


@pytest.mark.parametrize("ev", list(Ev))
def test_cannot_leave_fault_unless_rig_ok(ev: Ev) -> None:
    tr = transition(S.FAULT, ev, Ctx(rig_ok=False, tx_consumer_running=True))
    assert tr is None or tr.to is S.FAULT


def test_fault_exits() -> None:
    ok = Ctx(rig_ok=True)
    rec = transition(S.FAULT, Ev.RECOVERY_OK, ok)
    clr = transition(S.FAULT, Ev.FAULT_CLEAR, ok)
    assert rec and rec.to is S.RETURNING
    assert clr and clr.to is S.RETURNING and Act.RESET_FAULTS in clr.actions
    ex = transition(S.FAULT, Ev.RECOVERY_EXHAUSTED)
    assert ex and ex.to is S.FAULT and Act.LATCH in ex.actions


@pytest.mark.parametrize(
    ("st", "ev"),
    [
        (S.BOOT, Ev.LEASE_GRANTED),
        (S.SWITCHING, Ev.LEASE_GRANTED),
        (S.RETURNING, Ev.LEASE_GRANTED),
        (S.FAULT, Ev.LEASE_GRANTED),
        (S.DEFAULT, Ev.RELEASE),
        (S.DEFAULT, Ev.STEPS_OK),
        (S.ACTIVE, Ev.START),
        (S.RETURNING, Ev.RELEASE),
    ],
)
def test_invalid_events(st: RadioState, ev: Ev) -> None:
    assert transition(st, ev, Ctx(target_is_default=False, rig_ok=True)) is None


def test_table_rows_for_docs() -> None:
    rows = table_rows()
    assert ("boot", "start", "—", "switching", "switch_default") in rows
    assert len(rows) == sum(len(v) for v in TABLE.values())
