"""Scenario tests for RadioController against MockRig / mock services / mock jobs."""

from __future__ import annotations

import asyncio
import time

from rigbroker.jobs import MockJobRunner
from rigbroker.leases import Verdict
from rigbroker.state import RadioState as S
from tests.conftest import TX_SERVICES, MakeHarness, wait_until

APRS, PKT, VARA, MON = 144_390_000, 145_050_000, 145_090_000, 147_240_000


async def test_boot_applies_default_profile(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    assert (h.rig.freq, h.rig.mode) == (APRS, "FM")
    assert h.rig.levels["RFPOWER"] == 0.5 and h.rig.levels["SQL"] == 0.15
    assert h.consumers() == {"direwolf@aprs"}
    assert h.radio.applied == "aprs" and h.radio.consumer == "direwolf@aprs"


async def test_happy_path_default_active_default(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    r = await h.radio.request_lease("monitor", holder="web")
    assert r.verdict is Verdict.GRANT
    await h.wait_state(S.ACTIVE)
    assert h.rig.freq == MON
    assert h.consumers() == {"audio-stream"}
    ok_steps = [e.data["step"] for e in h.events.recent(0, kind="step") if e.data["ok"]]
    for step in ("wait_tx_idle", "stop_consumer", "set_rig", "verify", "start_consumer"):
        assert step in ok_steps
    assert all("ms" in e.data for e in h.events.recent(0, kind="step"))
    ok, _ = await h.radio.release()
    assert ok
    await h.wait_state(S.DEFAULT)
    assert h.rig.freq == APRS and h.consumers() == {"direwolf@aprs"}
    assert h.radio.lease is None
    assert h.transitions()[-4:] == [("default", "switching"), ("switching", "active"),
                                    ("active", "returning"), ("returning", "default")]


async def test_run_job_completion_returns_to_default(make: MakeHarness) -> None:
    h = await make(jobs=MockJobRunner(duration_s=0.1))
    await h.wait_state(S.DEFAULT)
    assert (await h.radio.request_lease("winlink_pkt", holder="web")).ok
    await wait_until(lambda: ("switching", "active") in h.transitions())
    await wait_until(lambda: h.jobs.runs == ["pat connect ax25:///N0CALL-10"])
    await wait_until(lambda: ("active", "returning") in h.transitions())
    await h.wait_state(S.DEFAULT)
    assert "direwolf@pkt" in h.services.started()
    assert "job_done" in h.messages("lease_ended")[-1]


async def test_refuses_to_switch_while_tx_then_switches_after_holdoff(
        make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    h.rig.ptt = True
    assert (await h.radio.request_lease("monitor", holder="web")).ok
    await asyncio.sleep(0.3)
    assert h.radio.state is S.SWITCHING
    assert h.rig.freq == APRS, "must not retune while transmitting"
    assert "direwolf@aprs" in h.services.active, "consumer stopped only after TX idle"
    released = time.monotonic()
    h.rig.ptt = False
    await h.wait_state(S.ACTIVE)
    assert time.monotonic() - released >= 0.1  # tx_holdoff_s
    assert h.rig.freq == MON


async def test_waits_for_clear_channel(make: MakeHarness) -> None:
    h = await make(jobs=MockJobRunner(duration_s=30))
    await h.wait_state(S.DEFAULT)
    h.rig.dcd = True
    assert (await h.radio.request_lease("winlink_pkt", holder="web")).ok
    await asyncio.sleep(0.15)
    assert h.radio.state is S.SWITCHING
    assert h.rig.freq == PKT
    assert "direwolf@pkt" not in h.services.started()
    h.rig.dcd = False
    await h.wait_state(S.ACTIVE)
    assert "direwolf@pkt" in h.services.active


async def test_clear_channel_timeout_returns_to_default(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    h.rig.dcd = True
    assert (await h.radio.request_lease("winlink_pkt", holder="web")).ok
    await h.wait_state(S.RETURNING)
    assert "clear_channel_timeout" in h.messages("lease_ended")[-1]
    assert h.radio.fault is None
    # default (aprs) also transmits: keeps waiting for a clear channel, consumer not started
    await wait_until(lambda: bool(h.messages("channel_busy")))
    assert h.radio.state is S.RETURNING and not h.tx_active()
    h.rig.dcd = False
    await h.wait_state(S.DEFAULT)
    assert h.consumers() == {"direwolf@aprs"}
    assert "direwolf@pkt" not in h.services.started()


async def test_readback_mismatch_faults_and_inhibits_tx(make: MakeHarness) -> None:
    h = await make(max_auto_recoveries=0)
    await h.wait_state(S.DEFAULT)
    h.rig.readback_override = {"RFPOWER": 0.9}
    assert (await h.radio.request_lease("winlink_pkt", holder="web")).ok
    await h.wait_state(S.FAULT)
    assert h.radio.fault and "read-back mismatch" in h.radio.fault.reason
    assert "RFPOWER 0.9 != 0.5" in h.radio.fault.reason
    await wait_until(lambda: not h.tx_active(), what="TX services stopped")
    assert "direwolf@pkt" not in h.services.started()
    assert h.radio.lease is None
    assert h.messages("alert")
    await wait_until(lambda: bool(h.radio.fault and h.radio.fault.latched))
    # stays in FAULT; lease requests refused
    r = await h.radio.request_lease("monitor", holder="web")
    assert r.verdict is Verdict.REJECT and "FAULT" in r.reason
    # fix and clear manually
    h.rig.readback_override = {}
    ok, msg = await h.radio.clear_fault()
    assert ok, msg
    await h.wait_state(S.DEFAULT)
    assert h.consumers() == {"direwolf@aprs"}
    assert h.radio.last_fault is not None


async def test_recovery_does_not_restart_rigctld_by_default(make: MakeHarness) -> None:
    h = await make(max_auto_recoveries=1)
    await h.wait_state(S.DEFAULT)
    h.rig.freq = 146_520_000  # drift -> fault -> recovery
    await h.wait_state(S.FAULT)
    await h.wait_state(S.DEFAULT, timeout=3)
    assert "rigctld-vu" not in h.services.started()  # no extra Digirig PTT pulse


async def test_repeated_faults_latch_after_budget(make: MakeHarness) -> None:
    h = await make(max_auto_recoveries=2, recovery_restart_rigctld=True)
    await h.wait_state(S.DEFAULT)
    h.rig.readback_override = {"RFPOWER": 0.9}  # every switch now fails verify
    assert (await h.radio.request_lease("vara_fm", holder="web")).ok
    await wait_until(lambda: bool(h.radio.fault and h.radio.fault.latched), timeout=5)
    faults = [t for t in h.transitions() if t[1] == "fault" and t[0] != "fault"]
    assert len(faults) == 3  # 2 automatic recoveries, the 3rd fault latches
    assert not h.tx_active()
    assert "rigctld-vu" in h.services.started()  # recovery restarted rigctld


async def test_stuck_tx_faults_and_recovers(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    h.rig.ptt = True
    await h.wait_state(S.FAULT, timeout=2)
    assert h.radio.fault and h.radio.fault.kind == "stuck_tx"
    h.rig.ptt = False
    await wait_until(lambda: not h.tx_active(), what="TX services stopped")
    await wait_until(lambda: "relay-off" in h.jobs.runs, what="rig_power_cut hook")
    await h.wait_state(S.DEFAULT, timeout=3)
    assert h.consumers() == {"direwolf@aprs"}


async def test_lease_expiry_returns_to_default(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    r = await h.radio.request_lease("monitor", holder="web", minutes=0.005)  # 0.3 s
    assert r.ok
    await h.wait_state(S.ACTIVE)
    await h.wait_state(S.DEFAULT, timeout=2)
    assert "lease_expired" in h.messages("lease_ended")[-1]


async def test_lease_minutes_clamped_to_profile_max(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    r = await h.radio.request_lease("winlink_pkt", holder="web", minutes=999)
    assert r.lease and r.lease.duration_s == 10 * 60


async def test_priority_rules_reject_queue_and_preempt(make: MakeHarness) -> None:
    h = await make(jobs=MockJobRunner(duration_s=30))
    await h.wait_state(S.DEFAULT)
    assert (await h.radio.request_lease("monitor", holder="a", priority="manual")).ok
    await h.wait_state(S.ACTIVE)

    r = await h.radio.request_lease("winlink_pkt", holder="sched", priority="scheduled")
    assert r.verdict is Verdict.REJECT and "not preemptible" in r.reason
    r = await h.radio.request_lease("vara_fm", holder="b", priority="manual")
    assert r.verdict is Verdict.REJECT and "held by a" in r.reason
    r = await h.radio.request_lease("vara_fm", holder="b", priority="manual", queue=True)
    assert r.verdict is Verdict.QUEUE

    await h.radio.release()
    await wait_until(lambda: h.radio.lease is not None and h.radio.lease.holder == "b")
    await h.wait_state(S.ACTIVE)
    assert h.radio.target == "vara_fm" and h.rig.freq == VARA


async def test_higher_priority_preempts_preemptible_profile(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    assert (await h.radio.request_lease("aprs_quiet", holder="sched",
                                        priority="scheduled")).ok
    await h.wait_state(S.ACTIVE)
    r = await h.radio.request_lease("monitor", holder="web", priority="manual")
    assert r.verdict is Verdict.PREEMPT
    await wait_until(lambda: h.radio.target == "monitor")
    await h.wait_state(S.ACTIVE)
    assert "preempts" in h.messages("lease_ended")[-1]
    # lower priority cannot take it back
    r = await h.radio.request_lease("aprs_quiet", holder="sched", priority="scheduled")
    assert r.verdict is Verdict.REJECT
    # the holder may replace its own lease
    r = await h.radio.request_lease("vara_fm", holder="web", priority="manual")
    assert r.verdict is Verdict.PREEMPT


async def test_lease_validation(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    assert "unknown profile" in (await h.radio.request_lease("nope", holder="x")).reason
    assert "default profile" in (await h.radio.request_lease("aprs", holder="x")).reason
    assert "priority" in (await h.radio.request_lease("monitor", holder="x",
                                                      priority="urgent")).reason
    assert not (await h.radio.release())[0]


async def test_rig_offline_with_tx_consumer_faults_then_recovers(make: MakeHarness) -> None:
    h = await make(recovery_backoff_s=0.2)
    await h.wait_state(S.DEFAULT)
    h.rig.offline = True
    await h.wait_state(S.FAULT)
    assert h.radio.fault and h.radio.fault.kind == "rig_offline"
    await wait_until(lambda: not h.tx_active())
    h.rig.offline = False
    await h.wait_state(S.DEFAULT, timeout=3)
    assert h.radio.telemetry.online and h.consumers() == {"direwolf@aprs"}


async def test_rig_offline_without_tx_consumer_is_not_a_fault(make: MakeHarness) -> None:
    h = await make(allow_tx=False)
    await h.wait_state(S.DEFAULT)
    await wait_until(lambda: h.radio.telemetry.online)
    h.rig.offline = True
    await wait_until(lambda: not h.radio.telemetry.online, what="offline detected")
    assert h.radio.state is S.DEFAULT
    assert h.messages("rig_offline")
    r = await h.radio.request_lease("monitor", holder="web")
    assert r.verdict is Verdict.REJECT and "offline" in r.reason
    h.rig.offline = False
    await wait_until(lambda: h.radio.telemetry.online, what="online again")
    assert h.messages("rig_online")
    assert (await h.radio.request_lease("monitor", holder="web")).ok
    await h.wait_state(S.ACTIVE)


async def test_without_allow_tx_tx_services_never_start(make: MakeHarness) -> None:
    h = await make(allow_tx=False, jobs=MockJobRunner(duration_s=0.05))
    await h.wait_state(S.DEFAULT)
    assert h.rig.freq == APRS  # reading/tuning is allowed
    # run-job profiles are refused up front: the radio must not bounce there and back
    r = await h.radio.request_lease("winlink_pkt", holder="web")
    assert not r.ok and "read-only" in r.reason and not r.invalid
    assert h.radio.state is S.DEFAULT and h.rig.freq == APRS
    # a TX profile with no receive-only fallback can't do anything useful either
    r = await h.radio.request_lease("vara_fm", holder="web")
    assert not r.ok and "read-only" in r.reason
    st = h.radio.status()["profiles"]
    assert st["vara_fm"]["unavailable"] and st["winlink_pkt"]["unavailable"]
    assert st["monitor"]["unavailable"] is None and st["aprs"]["unavailable"] is None
    assert (await h.radio.request_lease("monitor", holder="web")).ok
    await h.wait_state(S.ACTIVE)
    assert h.consumers() == {"audio-stream"}  # non-TX consumers are fine
    assert not set(h.services.started()) & TX_SERVICES
    assert h.jobs.runs == []
    assert h.messages("tx_inhibited")


async def test_profile_with_missing_service_is_unavailable(make: MakeHarness) -> None:
    h = await make(start=False)
    h.services.missing.add("vara-fm")  # e.g. VARA not installed yet
    await h.radio.start()
    await h.wait_state(S.DEFAULT)
    r = await h.radio.request_lease("vara_fm", holder="web")
    assert not r.ok and "vara-fm is not installed" in r.reason
    assert h.rig.freq == APRS  # never moved
    h.services.missing.clear()  # installed later: available on the next check
    assert (await h.radio.request_lease("vara_fm", holder="web")).ok
    await h.wait_state(S.ACTIVE)
    assert h.consumers() == {"vara-fm"}


async def test_rx_only_fallback_without_allow_tx(make: MakeHarness) -> None:
    h = await make(allow_tx=False)
    await h.wait_state(S.DEFAULT)
    assert h.consumers() == {"direwolf@aprs-rx"}  # APRS still receives, can't transmit
    assert h.radio.consumer == "direwolf@aprs-rx"
    assert not set(h.services.started()) & TX_SERVICES
    h.rig.offline = True  # RX-only consumer: going offline is not a fault
    await wait_until(lambda: not h.radio.telemetry.online)
    assert h.radio.state is S.DEFAULT


async def test_waits_for_rig_at_boot(make: MakeHarness) -> None:
    h = await make(start=False)
    h.rig.offline = True  # radio off / unplugged
    await h.radio.start()
    await asyncio.sleep(0.4)
    assert h.radio.state is S.BOOT and h.radio.fault is None
    r = await h.radio.request_lease("monitor", holder="t")
    assert not r.ok and "waiting for the rig" in r.reason
    h.rig.offline = False  # radio switched on
    await h.wait_state(S.DEFAULT)
    assert h.rig.freq == APRS


async def test_unexpected_tx_faults_immediately(make: MakeHarness) -> None:
    # read-only: APRS runs the RX-only Direwolf, so nothing of ours can key the rig
    h = await make(allow_tx=False, recovery_backoff_s=0.2)
    await h.wait_state(S.DEFAULT)
    h.rig.ptt = True  # e.g. a manual VARA "Tune" outside RigBroker
    await h.wait_state(S.FAULT, timeout=1)  # long before the stuck-TX watchdog
    assert h.radio.fault and h.radio.fault.kind == "unexpected_tx"
    assert "keyed outside RigBroker" in h.radio.fault.reason
    h.rig.ptt = False
    await h.wait_state(S.DEFAULT, timeout=3)


async def test_tx_from_our_own_consumer_is_fine(make: MakeHarness) -> None:
    h = await make()  # allow_tx: direwolf@aprs (TX-capable) runs
    await h.wait_state(S.DEFAULT)
    h.rig.ptt = True  # an APRS beacon
    await asyncio.sleep(0.2)
    h.rig.ptt = False
    await asyncio.sleep(0.1)
    assert h.radio.state is S.DEFAULT and h.radio.fault is None


async def test_frequency_drift_on_tx_profile_faults(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    h.rig.freq = 146_520_000  # someone turned the knob
    await h.wait_state(S.FAULT)
    assert h.radio.fault and h.radio.fault.kind == "rig_drift"
    await h.wait_state(S.DEFAULT, timeout=3)  # recovery re-applies the default profile
    assert h.rig.freq == APRS


async def test_drift_on_rx_only_profile_is_a_warning(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    assert (await h.radio.request_lease("monitor", holder="web")).ok
    await h.wait_state(S.ACTIVE)
    h.rig.freq = 146_520_000
    await wait_until(lambda: bool(h.messages("rig_drift")))
    assert h.radio.state is S.ACTIVE


async def test_rig_power_off_profile(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    assert (await h.radio.request_lease("off", holder="web")).ok
    await h.wait_state(S.ACTIVE)
    assert h.rig.power_on is False and not h.consumers()
    await wait_until(lambda: not h.radio.telemetry.polling)
    await h.radio.release()
    await h.wait_state(S.DEFAULT)
    assert h.rig.power_on is True and h.rig.freq == APRS
    assert h.consumers() == {"direwolf@aprs"}


async def test_service_start_failure_faults(make: MakeHarness) -> None:
    h = await make(max_auto_recoveries=0)
    await h.wait_state(S.DEFAULT)
    h.services.fail_start.add("vara-fm")
    assert (await h.radio.request_lease("vara_fm", holder="web")).ok
    await h.wait_state(S.FAULT)
    assert h.radio.fault and "vara-fm" in h.radio.fault.reason


async def test_stop_shuts_down_consumers(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    assert h.services.active
    await h.radio.stop()
    assert not h.consumers()


async def test_status_snapshot(make: MakeHarness) -> None:
    h = await make()
    await h.wait_state(S.DEFAULT)
    await wait_until(lambda: h.radio.telemetry.online)
    st = h.radio.status()
    assert st["state"] == "default" and st["applied_profile"] == "aprs"
    assert st["telemetry"]["freq"] == APRS and st["telemetry"]["online"] is True
    assert st["profiles"]["winlink_pkt"]["transmits"] is True
