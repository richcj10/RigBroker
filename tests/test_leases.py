from __future__ import annotations

import pytest

from rigbroker.leases import Holding, Lease, Priority, Verdict, arbitrate
from rigbroker.state import RadioState as S

M, SC, D = Priority.MANUAL, Priority.SCHEDULED, Priority.DEFAULT


def req(prio: Priority = M, holder: str = "web") -> Lease:
    return Lease(profile="monitor", holder=holder, priority=prio, duration_s=60)


def held(state: S = S.ACTIVE, prio: Priority = SC, preemptible: bool = False,
         holder: str = "sched") -> Holding:
    return Holding(state, "winlink_pkt", holder, prio, preemptible)


def test_default_state_grants() -> None:
    h = Holding(S.DEFAULT, "aprs", "default", D, True)
    assert arbitrate(h, req(SC), queue=False, rig_available=True).verdict is Verdict.GRANT


@pytest.mark.parametrize("state", [S.FAULT, S.BOOT])
def test_fault_and_boot_reject(state: S) -> None:
    d = arbitrate(held(state), req(), queue=True, rig_available=True)
    assert d.verdict is Verdict.REJECT


def test_offline_rejects() -> None:
    h = Holding(S.DEFAULT, "aprs", "default", D, True)
    d = arbitrate(h, req(), queue=True, rig_available=False)
    assert d.verdict is Verdict.REJECT and "offline" in d.reason


def test_default_priority_request_rejected() -> None:
    h = Holding(S.DEFAULT, "aprs", "default", D, True)
    assert arbitrate(h, req(D), queue=False, rig_available=True).verdict is Verdict.REJECT


def test_higher_priority_preempts_preemptible() -> None:
    d = arbitrate(held(prio=SC, preemptible=True), req(M), queue=False, rig_available=True)
    assert d.verdict is Verdict.PREEMPT


def test_not_preemptible_rejects_or_queues() -> None:
    d = arbitrate(held(prio=SC, preemptible=False), req(M), queue=False, rig_available=True)
    assert d.verdict is Verdict.REJECT and "not preemptible" in d.reason
    d = arbitrate(held(prio=SC, preemptible=False), req(M), queue=True, rig_available=True)
    assert d.verdict is Verdict.QUEUE


def test_equal_priority_does_not_preempt() -> None:
    d = arbitrate(held(prio=M, preemptible=True), req(M), queue=False, rig_available=True)
    assert d.verdict is Verdict.REJECT and "not higher" in d.reason


def test_lower_priority_never_preempts() -> None:
    d = arbitrate(held(prio=M, preemptible=True), req(SC), queue=False, rig_available=True)
    assert d.verdict is Verdict.REJECT


def test_holder_replaces_own_lease() -> None:
    d = arbitrate(held(prio=M, holder="web"), req(M, holder="web"), queue=False,
                  rig_available=True)
    assert d.verdict is Verdict.PREEMPT


@pytest.mark.parametrize("state", [S.SWITCHING, S.RETURNING])
def test_switching_queue_or_reject(state: S) -> None:
    assert arbitrate(held(state), req(), queue=False, rig_available=True).verdict \
        is Verdict.REJECT
    assert arbitrate(held(state), req(), queue=True, rig_available=True).verdict is Verdict.QUEUE


def test_priority_parse() -> None:
    assert Priority.parse("Manual") is M
    assert Priority.parse(1) is SC
    with pytest.raises(ValueError):
        Priority.parse("urgent")


def test_lease_remaining() -> None:
    lease = Lease("monitor", "x", M, duration_s=60)
    lease.started_mono = 100.0
    assert lease.remaining_s(now=130.0) == 30.0
    assert lease.remaining_s(now=500.0) == 0.0
