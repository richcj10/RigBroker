"""RadioController: owns one radio, executes state-machine actions and the switching steps.

All state changes go through :meth:`RadioController._fire_locked`, which consults the pure
table in :mod:`rigbroker.state` while holding the radio's asyncio lock. Long-running work
(switching, lease timers, jobs, fault recovery) runs in tasks that report back with events
tagged by a *generation* number, so results from superseded work are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from .config import Profile, RadioConfig, SafetyConfig
from .events import EventLog, Level
from .hwcheck import (
    AUDIO_MISSING,
    DIGIRIG_MISSING,
    FAILURE_LABELS,
    RIG_ERROR,
    InterfaceProbe,
    diagnose,
)
from .jobs import JobRunner
from .leases import Decision, Holding, Lease, PlanStep, Priority, Verdict, arbitrate
from .rig_client import RigClient, RigError
from .services import ServiceController, ServiceError, restart
from .state import Act, Ctx, Ev, RadioState, transition
from .telemetry import Sample, Telemetry, TelemetryPoller

log = logging.getLogger("rigbroker.radio")

T = TypeVar("T")

MIN_ATTEMPT_S = 30.0  # don't start another plan step with less lease time than this
# (radio_id, step, ok, duration_s, detail) -> None; e.g. the Winlink planner's history
AttemptListener = Callable[[str, PlanStep, bool, float, str], None]


class SwitchAbort(Exception):
    """A switching step could not complete."""


class TxNeverIdle(SwitchAbort):
    pass


class ReadbackMismatch(SwitchAbort):
    pass


class ClearChannelTimeout(SwitchAbort):
    pass


class ConsumerError(SwitchAbort):
    pass


@dataclass(frozen=True)
class LeaseResult:
    """Outcome of a lease request."""

    verdict: Verdict
    reason: str
    lease: Lease | None = None
    invalid: bool = False  # True = bad request (unknown profile etc.), not a conflict

    @property
    def ok(self) -> bool:
        return self.verdict is not Verdict.REJECT


@dataclass
class FaultInfo:
    reason: str
    kind: str
    ts: float
    latched: bool = False
    recovery_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class RadioController:
    """Single owner of one radio."""

    def __init__(
        self,
        radio_id: str,
        cfg: RadioConfig,
        safety: SafetyConfig,
        rig: RigClient,
        services: ServiceController,
        jobs: JobRunner,
        events: EventLog,
        *,
        allow_tx: bool = False,
        attempt_listener: AttemptListener | None = None,
        probe: InterfaceProbe | None = None,
    ) -> None:
        self.id = radio_id
        self.probe = probe
        self.attempt_listener = attempt_listener
        self.cfg = cfg
        self.safety = safety
        self.rig = rig
        self.services = services
        self.jobs = jobs
        self.events = events
        self.allow_tx = allow_tx

        self.state = RadioState.BOOT
        self.state_since = time.monotonic()
        self.applied: str | None = None  # profile currently applied to (and verified on) the rig
        self.applied_freq: int | None = None  # verified frequency (plan steps override profile)
        self.target: str | None = None  # profile being switched to / held
        self.lease: Lease | None = None
        self.queue: list[Lease] = []
        self.consumer: str | None = None
        self.fault: FaultInfo | None = None
        self.last_fault: FaultInfo | None = None
        self.telemetry = Telemetry()
        self.poller = TelemetryPoller(rig, safety, self.telemetry, self)
        self.unavailable: dict[str, str] = {}  # profile -> why it can't run right now

        self._lock = asyncio.Lock()
        self._gen = 0
        self._pending: Lease | None = None
        self._fault_times: deque[float] = deque()
        self._consumer_restarts: deque[float] = deque()
        self._last_once: dict[str, str] = {}
        self._drift_warned = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._switch_task: asyncio.Task[None] | None = None
        self._lease_task: asyncio.Task[None] | None = None
        self._job_task: asyncio.Task[None] | None = None
        self._fault_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Connect and start telemetry. The default profile is applied once the rig first
        answers (a radio that's off or unplugged is "waiting", not a fault)."""
        try:
            await self.rig.connect()
        except RigError as exc:
            self._emit("rig", f"rigctld not reachable yet: {exc}", "warning")
        self._emit("boot", "waiting for the rig to answer before applying the default profile")
        await self.refresh_availability()
        self.check_interface()
        self._spawn(self._availability_loop(), "availability")
        self._spawn(self._consumer_watchdog(), "consumer-watchdog")
        self._poll_task = self._spawn(self.poller.run(), "poller")

    # ------------------------------------------------------------------ availability

    def _service_to_run(self, prof: Profile) -> str | None:
        """The consumer a profile would actually start under the current TX permission."""
        if not prof.service:
            return None
        if prof.transmits and not self.allow_tx:
            return prof.rx_service
        return prof.service

    async def refresh_availability(self) -> dict[str, str]:
        """Which profiles can't do anything useful right now, and why."""
        out: dict[str, str] = {}
        for name, p in self.cfg.profiles.items():
            tx_only = p.run or p.winlink_mode or (p.transmits and not p.rx_service)
            if not self.allow_tx and tx_only:
                out[name] = "needs to transmit; the broker is read-only (--allow-tx not set)"
                continue
            svc = self._service_to_run(p)
            if svc:
                try:
                    if not await self.services.exists(svc):
                        out[name] = f"{svc} is not installed"
                except ServiceError as exc:
                    out[name] = f"can't check {svc}: {exc}"
        if out != self.unavailable:
            self.unavailable = out
            self._emit("availability", "unavailable profiles: " + (
                "; ".join(f"{k} ({v})" for k, v in sorted(out.items())) or "none"))
        return out

    async def _availability_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.refresh_availability()  # e.g. VARA installed later

    CONSUMER_CHECK_S = 15.0
    CONSUMER_MAX_RESTARTS = 3  # within safety.fault_window_s

    async def _consumer_watchdog(self) -> None:
        while True:
            await asyncio.sleep(self.CONSUMER_CHECK_S)
            try:
                self.check_interface()
                await self.check_consumer()
            except Exception:  # never let a watchdog bug kill the loop
                log.exception("consumer watchdog failed; continuing")

    def check_interface(self) -> dict[str, bool | None] | None:
        """Track the USB interface's serial chip and sound card; log when either comes
        or goes (a missing sound card stops Direwolf/VARA even while CI-V still works)."""
        if self.probe is None:
            return None
        snap = self.probe.snapshot()
        prev = self.telemetry.interface or {}
        name = self.probe.cfg.name
        if snap["serial"] is not None and snap["serial"] != prev.get("serial"):
            if snap["serial"]:
                self._emit("interface", f"{name} serial (CI-V/PTT) detected on USB")
            else:
                self._emit(DIGIRIG_MISSING, f"{name} serial (CI-V/PTT) NOT detected on USB "
                           f"(serial {self.probe.cfg.usb_serial})", "error")
        if snap["audio"] is not None and snap["audio"] != prev.get("audio"):
            if snap["audio"]:
                self._emit("interface", f"{name} sound card '{self.probe.cfg.audio_card}' "
                           "detected")
            else:
                self._emit(AUDIO_MISSING, f"{name} sound card '{self.probe.cfg.audio_card}' "
                           "NOT detected: Direwolf/VARA can't receive or transmit", "error")
        self.telemetry.interface = snap
        return snap

    async def check_consumer(self) -> str:
        """Notice a consumer that died on its own (e.g. Direwolf losing the sound card when
        the Digirig is unplugged) and restart it, a few times at most. Returns what it did."""
        async with self._lock:
            svc = self.consumer
            if svc is None or self.state not in (RadioState.DEFAULT, RadioState.ACTIVE):
                return "idle"
            try:
                if await self.services.is_active(svc):
                    return "ok"
            except ServiceError:
                return "unknown"
            if not self.telemetry.online:
                # rig unplugged too: restarting now would just fail again; retry once it's back
                self._emit_once("consumer_down",
                                f"{svc} stopped; will restart it when the rig is back", "warning")
                return "waiting"
            now = time.monotonic()
            while self._consumer_restarts and now - self._consumer_restarts[0] > \
                    self.safety.fault_window_s:
                self._consumer_restarts.popleft()
            if len(self._consumer_restarts) >= self.CONSUMER_MAX_RESTARTS:
                self.consumer = None
                self._emit("consumer_down", f"{svc} keeps stopping; gave up after "
                           f"{self.CONSUMER_MAX_RESTARTS} restarts - check its log "
                           f"(journalctl -u {svc}), then select the profile again", "error")
                return "gave_up"
            self._consumer_restarts.append(now)
            try:
                await self._wait_tx_idle(self.safety.tx_holdoff_s)
                await self.services.start(svc)
                if not await self.services.is_active(svc):
                    raise ConsumerError(f"{svc} did not start")
            except (RigError, ServiceError, SwitchAbort) as exc:
                self._emit("consumer_down", f"{svc} stopped on its own; restart failed: {exc}",
                           "error")
                return "failed"
            self._last_once.pop("consumer_down", None)
            self._emit("consumer_restart", f"{svc} had stopped on its own; restarted it",
                       "warning")
            return "restarted"

    def _emit_once(self, kind: str, message: str, level: Level = "info") -> None:
        """Emit an event only when its message changes (avoid repeating every check)."""
        if self._last_once.get(kind) != message:
            self._last_once[kind] = message
            self._emit(kind, message, level)

    async def stop(self) -> None:
        """Cancel all work, stop every consumer this radio owns, close the rig."""
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for svc in sorted(self.cfg.all_services()):
            with contextlib.suppress(ServiceError):
                if await self.services.is_active(svc):
                    await self.services.stop(svc)
        self.consumer = None
        with contextlib.suppress(RigError):
            await self.rig.close()
        self._emit("shutdown", "radio controller stopped")

    # ------------------------------------------------------------------ public commands

    async def request_lease(
        self,
        profile: str,
        *,
        holder: str,
        priority: str | int | Priority = Priority.MANUAL,
        minutes: float | None = None,
        queue: bool = False,
        plan: list[PlanStep] | None = None,
    ) -> LeaseResult:
        """Ask for a profile. Returns granted/preempted/queued/rejected with a reason.

        ``plan`` (optional) is an ordered list of attempts, each with its own frequency and
        command; frequencies must lie in the profile's ``freq_ranges``. A failed attempt
        moves on to the next step inside the same lease.
        """
        if profile in self.cfg.profiles and profile != self.cfg.default_profile:
            await self.refresh_availability()
        async with self._lock:
            if profile not in self.cfg.profiles:
                return self._reject(f"unknown profile {profile!r} "
                                    f"(have: {', '.join(sorted(self.cfg.profiles))})",
                                    invalid=True)
            if profile == self.cfg.default_profile:
                return self._reject(f"{profile!r} is the default profile; release to return to it",
                                    invalid=True)
            try:
                prio = Priority.parse(priority)
            except ValueError as exc:
                return self._reject(str(exc), invalid=True)
            prof = self.cfg.profiles[profile]
            why = self.unavailable.get(profile)
            if why:  # it couldn't do anything useful: don't move the radio
                return self._reject(f"{profile!r} unavailable: {why}")
            max_min = prof.max_minutes or self.safety.default_lease_minutes
            mins = max_min if minutes is None else minutes
            if mins <= 0:
                return self._reject("minutes must be > 0", invalid=True)
            mins = min(mins, max_min)
            if plan:
                if not prof.freq_ranges:
                    return self._reject(f"profile {profile!r} has no freq_ranges; per-attempt "
                                        "frequencies are not allowed", invalid=True)
                for step in plan:
                    if not prof.freq_allowed(step.freq):
                        return self._reject(f"{step.label}: {step.freq} Hz is outside the allowed "
                                            f"ranges of profile {profile!r}", invalid=True)
            elif prof.winlink_mode:
                return self._reject(f"profile {profile!r} needs a gateway plan: use Winlink check",
                                    invalid=True)
            lease = Lease(profile=profile, holder=holder, priority=prio, duration_s=mins * 60,
                          plan=list(plan or []))
            return self._submit_locked(lease, queue)

    async def release(self, by: str = "user") -> tuple[bool, str]:
        """End the current lease and return to the default profile."""
        async with self._lock:
            if self.lease is None:
                return False, "no active lease"
            if self._fire_locked(Ev.RELEASE, reason=f"released by {by}"):
                return True, "returning to default"
            return False, f"cannot release in state {self.state.value}"

    async def clear_fault(self, by: str = "user") -> tuple[bool, str]:
        """Manually clear a FAULT once the rig answers and is in RX."""
        async with self._lock:
            if self.state is not RadioState.FAULT:
                return False, "radio is not in FAULT"
            try:
                await self.rig.connect()
                ptt = await self.rig.get_ptt()
            except RigError as exc:
                return False, f"rig not responding: {exc}"
            if ptt:
                return False, "rig is transmitting; refusing to clear"
            self.telemetry.online, self.telemetry.ptt = True, False
            if self._fire_locked(Ev.FAULT_CLEAR, reason=f"fault cleared by {by}"):
                return True, "fault cleared; returning to default"
            return False, "fault clear refused"

    async def restart_consumer(self, reason: str) -> tuple[bool, str]:
        """Restart the running consumer (e.g. after a config change), safely.

        Only in DEFAULT/ACTIVE, only after the rig has been in RX for the holdoff, under the
        radio lock so no switch can interleave. Never starts a consumer that wasn't running.
        """
        async with self._lock:
            svc = self.consumer
            if svc is None:
                return False, "no consumer running; the new config is used on its next start"
            if self.state not in (RadioState.DEFAULT, RadioState.ACTIVE):
                return False, f"radio is {self.state.value}; restart skipped"
            try:
                await self._wait_tx_idle(self.safety.tx_holdoff_s)
                await self.services.stop(svc)
                await self.services.start(svc)
                if not await self.services.is_active(svc):
                    raise ConsumerError(f"{svc} did not come back")
            except (RigError, ServiceError, SwitchAbort) as exc:
                if not await self.services.is_active(svc):
                    self.consumer = None
                self._emit("consumer_restart", f"restart of {svc} FAILED: {exc}", "error")
                return False, f"restart of {svc} failed: {exc}"
            self._emit("consumer_restart", f"restarted {svc}: {reason}")
            return True, f"restarted {svc}"

    def status(self) -> dict[str, Any]:
        """JSON-serialisable snapshot."""
        lease = self.lease
        return {
            "id": self.id,
            "name": self.cfg.name,
            "state": self.state.value,
            "state_for_s": round(time.monotonic() - self.state_since, 1),
            "applied_profile": self.applied,
            "applied_freq": self.applied_freq,
            "target_profile": self.target,
            "default_profile": self.cfg.default_profile,
            "winlink_profile": self.cfg.winlink_profile,
            "lease": lease.to_dict() if lease else None,
            "queue": [q.to_dict() for q in self.queue],
            "consumer": self.consumer,
            "allow_tx": self.allow_tx,
            "fault": self.fault.to_dict() if self.fault else None,
            "last_fault": self.last_fault.to_dict() if self.last_fault else None,
            "telemetry": self.telemetry.to_dict(),
            "profiles": {
                n: {"freq": p.freq, "mode": p.mode, "power": p.power, "squelch": p.squelch,
                    "service": p.service, "run": p.run,
                    "transmits": p.transmits, "preemptible": p.preemptible,
                    "max_minutes": p.max_minutes, "rig_power": p.rig_power,
                    "winlink_mode": p.winlink_mode, "unavailable": self.unavailable.get(n)}
                for n, p in self.cfg.profiles.items()
            },
        }

    # ------------------------------------------------------------------ poller callbacks

    def should_poll(self) -> bool:
        return not self._rig_is_off()

    async def on_sample(self, sample: Sample) -> None:
        async with self._lock:
            if (self.state in (RadioState.DEFAULT, RadioState.ACTIVE) and self.applied
                    and sample.started_mono >= self.state_since):
                if sample.ptt and self.consumer not in self.cfg.tx_services():
                    # Nothing RigBroker runs can transmit right now, yet the rig is keyed:
                    # someone/something keyed it outside the broker's control.
                    self._fire_locked(Ev.UNEXPECTED_TX, reason=(
                        f"rig transmitting but no TX-capable consumer is running "
                        f"(profile {self.applied}, consumer {self.consumer or 'none'}): "
                        "keyed outside RigBroker?"))
                    return
                self._check_drift(sample)
            lease = self.lease
            if (self.state is RadioState.ACTIVE and lease is not None
                    and lease.started_mono is not None
                    and time.monotonic() - lease.started_mono
                    > lease.duration_s + self.safety.lease_grace_s):
                self._fire_locked(Ev.LEASE_EXPIRED, reason="lease max time reached (watchdog)")

    def diagnose(self, exc: RigError) -> tuple[str, str]:
        d = diagnose(exc, probe=self.probe, rigctld=self.cfg.rigctld, radio=self.cfg.name)
        return d.code, d.message

    async def on_failure_changed(self, code: str, message: str) -> None:
        self._emit(code, f"still offline, now: {FAILURE_LABELS.get(code, code)} - {message}",
                   "warning", failure=code)

    async def on_offline(self, reason: str) -> None:
        code = self.telemetry.failure or RIG_ERROR
        async with self._lock:
            self._emit("rig_offline", f"rig offline - {FAILURE_LABELS.get(code, code)}: {reason}",
                       "warning", failure=code)
            self._fire_locked(Ev.RIG_OFFLINE, reason=f"rig offline while TX consumer running: "
                                                     f"{reason}")

    async def on_online(self) -> None:
        self._emit("rig_online", "rig online: interface, rigctld and radio all answering")
        if self.state is RadioState.BOOT:
            await self._fire(Ev.START, reason="rig answered")

    async def on_stuck_tx(self, seconds: float) -> None:
        async with self._lock:
            self._fire_locked(Ev.STUCK_TX, reason=f"rig transmitting for {seconds:.1f}s "
                                                  f"(limit {self.safety.stuck_tx_s:g}s)")

    # ------------------------------------------------------------------ state machine core

    async def _fire(self, event: Ev, *, gen: int | None = None, reason: str = "") -> bool:
        async with self._lock:
            return self._fire_locked(event, gen=gen, reason=reason)

    def _ctx(self) -> Ctx:
        t = self.telemetry
        lease = self.lease
        more = (lease is not None and lease.attempt + 1 < len(lease.plan)
                and lease.remaining_s() >= MIN_ATTEMPT_S)
        return Ctx(
            target_is_default=self.target == self.cfg.default_profile,
            tx_consumer_running=self.consumer is not None
            and self.consumer in self.cfg.tx_services(),
            rig_ok=t.online and t.ptt is False,
            more_candidates=more,
        )

    def _fire_locked(self, event: Ev, *, gen: int | None = None, reason: str = "") -> bool:
        """Apply one event. Returns False if stale or not valid in the current state."""
        if gen is not None and gen != self._gen:
            self._emit("stale_event", f"ignored stale {event.value} ({reason})", "debug")
            return False
        tr = transition(self.state, event, self._ctx())
        if tr is None:
            self._emit("ignored_event", f"{event.value} not applicable in {self.state.value}"
                       + (f" ({reason})" if reason else ""), "debug")
            return False
        old = self.state
        self.state = tr.to
        # New generation on every state change, and when a plan step supersedes the last
        # one (SWITCHING -> SWITCHING), so results from superseded work are ignored.
        if tr.to is not old or Act.NEXT_CANDIDATE in tr.actions:
            self.state_since = time.monotonic()
            self._gen += 1
        self._emit("transition", f"{old.value} -> {tr.to.value} on {event.value}"
                   + (f": {reason}" if reason else ""),
                   "warning" if tr.to is RadioState.FAULT else "info",
                   frm=old.value, to=tr.to.value, ev=event.value, reason=reason)
        if old is RadioState.FAULT and tr.to is not RadioState.FAULT:
            self.last_fault, self.fault = self.fault, None
            if self._fault_task is not None and self._fault_task is not asyncio.current_task():
                self._fault_task.cancel()
            self._fault_task = None
        for act in tr.actions:
            self._do(act, event, reason, recover=Act.RECOVER in tr.actions)
        return True

    def _do(self, act: Act, event: Ev, reason: str, *, recover: bool) -> None:
        if act is Act.SWITCH_DEFAULT:
            self.target = self.cfg.default_profile
            self._start_switch(self.cfg.default_profile)
        elif act is Act.SWITCH_TARGET:
            lease, self._pending = self._pending, None
            assert lease is not None, "SWITCH_TARGET without pending lease"
            lease.started_mono = time.monotonic()
            self.lease, self.target = lease, lease.profile
            self._emit("lease_started", f"lease {lease.id}: {lease.profile} for {lease.holder} "
                       f"({lease.priority.name.lower()}, {lease.duration_s / 60:.1f} min)",
                       lease=lease.to_dict())
            self._start_switch(lease.profile)
        elif act is Act.START_LEASE:
            self._start_lease()
        elif act is Act.NEXT_CANDIDATE:
            self._next_candidate(reason)
        elif act is Act.END_LEASE:
            self._end_lease(f"{event.value}: {reason}" if reason else event.value)
        elif act is Act.DEQUEUE:
            self._spawn(self._dequeue(), "dequeue")
        elif act is Act.CLEAR_QUEUE:
            for q in self.queue:
                self._emit("lease_dropped", f"queued lease {q.id} ({q.profile}) dropped: fault")
            self.queue.clear()
        elif act is Act.TX_INHIBIT:
            self._begin_fault_handling(event, reason, recover=recover)
        elif act is Act.ALERT:
            self._emit("alert", f"FAULT: {reason or event.value}", "alert", ev=event.value)
        elif act is Act.LATCH:
            if self.fault:
                self.fault.latched = True
        elif act is Act.RESET_FAULTS:
            self._fault_times.clear()
        # Act.RECOVER is handled together with TX_INHIBIT.

    # ------------------------------------------------------------------ leases

    def _reject(self, reason: str, *, invalid: bool = False) -> LeaseResult:
        self._emit("lease_rejected", reason, "warning")
        return LeaseResult(Verdict.REJECT, reason, invalid=invalid)

    def _holding(self) -> Holding:
        lease = self.lease
        if self.state is RadioState.ACTIVE and lease is not None:
            return Holding(self.state, lease.profile, lease.holder, lease.priority,
                           self.cfg.profiles[lease.profile].preemptible)
        if self.state is RadioState.DEFAULT:
            return Holding(self.state, self.cfg.default_profile, "default", Priority.DEFAULT, True)
        return Holding(self.state, self.target, lease.holder if lease else None,
                       lease.priority if lease else Priority.DEFAULT, False)

    def _rig_available(self) -> bool:
        return self.telemetry.online or self._rig_is_off()

    def _submit_locked(self, lease: Lease, queue: bool) -> LeaseResult:
        d: Decision = arbitrate(self._holding(), lease, queue=queue,
                                rig_available=self._rig_available())
        if d.verdict in (Verdict.GRANT, Verdict.PREEMPT):
            self._pending = lease
            if not self._fire_locked(Ev.LEASE_GRANTED, reason=d.reason):
                self._pending = None
                return self._reject(f"internal: grant not applicable in {self.state.value}")
            return LeaseResult(d.verdict, d.reason, lease)
        if d.verdict is Verdict.QUEUE:
            if len(self.queue) >= self.safety.max_queue:
                return self._reject("lease queue is full")
            self.queue.append(lease)
            self._emit("lease_queued", f"lease {lease.id} ({lease.profile}) queued: {d.reason}")
            return LeaseResult(d.verdict, d.reason, lease)
        return self._reject(d.reason)

    async def _dequeue(self) -> None:
        async with self._lock:
            if self.state is not RadioState.DEFAULT or not self.queue:
                return
            now = time.monotonic()
            for q in [q for q in self.queue if now - q.created_mono > q.duration_s]:
                self.queue.remove(q)
                self._emit("lease_dropped", f"queued lease {q.id} ({q.profile}) expired in queue")
            if not self.queue:
                return
            self.queue.sort(key=lambda q: (-q.priority, q.created_mono))
            lease = self.queue.pop(0)
            self._emit("lease_dequeued", f"starting queued lease {lease.id} ({lease.profile})")
            self._submit_locked(lease, queue=False)

    def _start_lease(self) -> None:
        lease = self.lease
        if lease is None or self.target is None:
            return
        gen = self._gen
        self._lease_task = self._spawn(self._lease_timer(lease, gen), f"lease-{lease.id}")
        prof = self.cfg.profiles[self.target]
        step = lease.current_step()
        cmd = step.run if step else prof.run
        if cmd:
            if prof.transmits and not self.allow_tx:
                self._emit("tx_inhibited", f"--allow-tx not set: not running {cmd!r}",
                           "warning")
                self._spawn(self._fire(Ev.JOB_DONE, gen=gen,
                                       reason="run job skipped (--allow-tx not set)"), "job-skip")
            else:
                self._job_task = self._spawn(self._run_job(cmd, lease, gen, step),
                                             f"job-{lease.id}")

    def _next_candidate(self, reason: str) -> None:
        lease = self.lease
        assert lease is not None and lease.attempt + 1 < len(lease.plan)
        cur = asyncio.current_task()
        for t in (self._lease_task, self._job_task):
            if t is not None and t is not cur and not t.done():
                t.cancel()
        self._lease_task = self._job_task = None
        failed = lease.plan[lease.attempt]
        lease.attempt += 1
        step = lease.plan[lease.attempt]
        self._emit("plan_next", f"{failed.label} failed ({reason}); trying {step.label} "
                   f"({lease.attempt + 1}/{len(lease.plan)}) at {step.freq} Hz",
                   "warning", failed=failed.key, next=step.key)
        self._start_switch(lease.profile)

    def _target_freq(self, target: str) -> int | None:
        """Frequency for a switch: the lease's current plan step, else the profile's."""
        lease = self.lease
        step = lease.current_step() if lease is not None and lease.profile == target else None
        return step.freq if step else self.cfg.profiles[target].freq

    def _end_lease(self, reason: str) -> None:
        lease, self.lease = self.lease, None
        cur = asyncio.current_task()
        for t in (self._lease_task, self._job_task):
            if t is not None and t is not cur and not t.done():
                t.cancel()
        self._lease_task = self._job_task = None
        if lease is not None:
            self._emit("lease_ended", f"lease {lease.id} ({lease.profile}) ended: {reason}",
                       lease=lease.to_dict(), reason=reason)

    async def _lease_timer(self, lease: Lease, gen: int) -> None:
        await asyncio.sleep(lease.remaining_s())
        await self._fire(Ev.LEASE_EXPIRED, gen=gen,
                         reason=f"lease {lease.id} reached {lease.duration_s / 60:.1f} min")

    async def _run_job(self, cmd: str, lease: Lease, gen: int, step: PlanStep | None) -> None:
        t0 = time.monotonic()
        self._emit("step", f"7/7 run_job started: {cmd}", step="run_job")
        timeout = lease.remaining_s()
        if step is not None:
            timeout = min(timeout, step.timeout_s)
        res = await self.jobs.run(cmd, timeout_s=max(1.0, timeout))
        dur = time.monotonic() - t0
        ms = round(dur * 1000)
        self._emit("job_output", res.output[-4000:], "info" if res.ok else "warning",
                   ok=res.ok, returncode=res.returncode, timed_out=res.timed_out, ms=ms)
        failed = "timed out" if res.timed_out else f"failed rc={res.returncode}"
        status = "ok" if res.ok else failed
        if step is not None and self.attempt_listener is not None:
            try:
                self.attempt_listener(self.id, step, res.ok, dur, status)
            except Exception as exc:  # history must never break the radio
                self._emit("internal_error", f"attempt listener failed: {exc!r}", "error")
        await self._fire(Ev.JOB_DONE if res.ok else Ev.JOB_FAILED, gen=gen,
                         reason=f"run job {status} ({ms} ms)")

    # ------------------------------------------------------------------ switching

    def _rig_is_off(self) -> bool:
        return self.applied is not None and self.cfg.profiles[self.applied].rig_power == "off"

    def _start_switch(self, target: str) -> None:
        prev = self._switch_task
        if prev is not None and not prev.done() and prev is not asyncio.current_task():
            prev.cancel()
        self._switch_task = self._spawn(self._run_switch(target, self._gen, prev),
                                        f"switch-{target}")

    async def _step(self, num: int, name: str, aw: Awaitable[T]) -> T:
        t0 = time.monotonic()
        try:
            result = await aw
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ms = round((time.monotonic() - t0) * 1000)
            self._emit("step", f"{num}/7 {name} FAILED after {ms} ms: {exc}", "error",
                       step=name, ms=ms, ok=False)
            raise
        ms = round((time.monotonic() - t0) * 1000)
        self._emit("step", f"{num}/7 {name} ok ({ms} ms)", step=name, ms=ms, ok=True)
        return result

    async def _run_switch(self, target: str, gen: int,
                          prev: asyncio.Task[None] | None) -> None:
        if prev is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prev
        prof = self.cfg.profiles[target]
        freq = self._target_freq(target)
        is_default = target == self.cfg.default_profile
        t0 = time.monotonic()
        lease = self.lease
        step = lease.current_step() if lease is not None and lease.profile == target else None
        self._emit("switch", f"switching to {target}"
                   + (f" via {step.label} at {freq} Hz" if step else ""), target=target)
        try:
            rig_off = self._rig_is_off()
            await self._step(1, "wait_tx_idle",
                             self._wait_tx_idle(self.safety.tx_holdoff_s, skip=rig_off))
            await self._step(2, "stop_consumer", self._stop_consumers(confirm_rx=not rig_off))
            self.applied = self.applied_freq = None
            await self._step(3, "set_rig", self._apply(prof, rig_off, freq))
            await self._step(4, "verify", self._verify(prof, freq))
            self.applied, self.applied_freq = target, freq
            self._drift_warned = False
            if prof.transmits:
                await self._step(5, "clear_channel", self._wait_clear(is_default))
            await self._step(6, "start_consumer", self._start_consumer(prof))
        except asyncio.CancelledError:
            self._emit("switch", f"switch to {target} cancelled", "warning")
            raise
        except ClearChannelTimeout as exc:
            await self._fire(Ev.CLEAR_CHANNEL_TIMEOUT, gen=gen, reason=str(exc))
            return
        except Exception as exc:  # any failure while switching is a fault (TX inhibit)
            await self._fire(Ev.STEP_FAILED, gen=gen, reason=f"switch to {target}: {exc}")
            return
        ms = round((time.monotonic() - t0) * 1000)
        await self._fire(Ev.STEPS_OK, gen=gen, reason=f"{target} ready in {ms} ms")

    async def _wait_tx_idle(self, holdoff: float, *, skip: bool = False) -> None:
        """Return once the rig has reported RX continuously for ``holdoff`` seconds."""
        if skip:
            return
        deadline = time.monotonic() + self.safety.stuck_tx_s + holdoff
        interval = min(0.2, max(holdoff / 4, 0.01))
        idle_since: float | None = None
        while True:
            ptt = await self.rig.get_ptt()
            now = time.monotonic()
            if ptt:
                idle_since = None
            elif idle_since is None:
                idle_since = now
            if idle_since is not None and now - idle_since >= holdoff:
                return
            if now > deadline:
                raise TxNeverIdle(f"rig still transmitting after {now - deadline + holdoff:.0f}s")
            await asyncio.sleep(interval)

    async def _stop_consumers(self, *, confirm_rx: bool) -> None:
        for svc in sorted(self.cfg.all_services()):
            if await self.services.is_active(svc):
                await self.services.stop(svc)
                if await self.services.is_active(svc):
                    raise ConsumerError(f"{svc} still active after stop")
        self.consumer = None
        if confirm_rx and await self.rig.get_ptt():
            # A consumer keyed up between the holdoff and its stop: wait again.
            await self._wait_tx_idle(self.safety.tx_holdoff_s)

    async def _apply(self, prof: Profile, rig_off: bool, freq: int | None) -> None:
        if prof.rig_power == "off":
            await self.rig.set_powerstat(False)
            return
        if rig_off:
            await self.rig.set_powerstat(True)
            await self._wait_rig_ready()
        assert freq is not None and prof.mode is not None
        await self.rig.set_freq(freq)
        await self.rig.set_mode(prof.mode, 0)
        if prof.power is not None:
            await self.rig.set_level("RFPOWER", prof.power)
        if prof.squelch is not None:
            await self.rig.set_level("SQL", prof.squelch)

    async def _wait_rig_ready(self) -> None:
        deadline = time.monotonic() + self.safety.power_on_timeout_s
        while True:
            try:
                await self.rig.get_freq()
                return
            except RigError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.25)

    async def _verify(self, prof: Profile, want_freq: int | None) -> None:
        if prof.rig_power == "off":
            return
        s = self.safety
        bad: list[str] = []
        freq = await self.rig.get_freq()
        if want_freq is not None and abs(freq - want_freq) > s.freq_tolerance_hz:
            bad.append(f"freq {freq} != {want_freq}")
        mode, _ = await self.rig.get_mode()
        if prof.mode is not None and mode != prof.mode:
            bad.append(f"mode {mode} != {prof.mode}")
        if prof.power is not None:
            p = await self.rig.get_level("RFPOWER")
            if abs(p - prof.power) > s.level_tolerance:
                bad.append(f"RFPOWER {p:g} != {prof.power:g}")
        if bad:
            raise ReadbackMismatch("read-back mismatch: " + "; ".join(bad))

    async def _wait_clear(self, is_default: bool) -> None:
        s = self.safety
        interval = min(0.2, max(s.clear_channel_hold_s / 4, 0.01))
        while True:
            end = time.monotonic() + s.clear_channel_timeout_s
            clear_since: float | None = None
            while time.monotonic() < end:
                busy = await self.rig.get_dcd()
                now = time.monotonic()
                if busy:
                    clear_since = None
                elif clear_since is None:
                    clear_since = now
                if clear_since is not None and now - clear_since >= s.clear_channel_hold_s:
                    return
                await asyncio.sleep(interval)
            msg = f"channel busy for {s.clear_channel_timeout_s:g}s"
            if not is_default:
                raise ClearChannelTimeout(msg)
            self._emit("channel_busy", f"{msg}; default profile keeps waiting "
                       "(consumer not started yet)", "warning")

    async def _start_consumer(self, prof: Profile) -> None:
        svc = prof.service
        if not svc:
            return
        if prof.transmits and not self.allow_tx:
            if prof.rx_service:
                self._emit("tx_inhibited", f"--allow-tx not set: running receive-only "
                           f"{prof.rx_service} instead of {svc}", service=prof.rx_service)
                svc = prof.rx_service
            else:
                self._emit("tx_inhibited", f"--allow-tx not set: not starting TX-capable "
                           f"{svc}", "warning", service=svc)
                return
        await self.services.start(svc)
        if not await self.services.is_active(svc):
            raise ConsumerError(f"{svc} did not become active")
        self.consumer = svc

    def _check_drift(self, sample: Sample) -> None:
        assert self.applied is not None
        prof = self.cfg.profiles[self.applied]
        want = self.applied_freq
        if prof.rig_power == "off" or want is None:
            return
        bad = []
        if abs(sample.freq - want) > self.safety.freq_tolerance_hz:
            bad.append(f"freq {sample.freq} != {want}")
        if prof.mode and sample.mode != prof.mode:
            bad.append(f"mode {sample.mode} != {prof.mode}")
        if not bad:
            return
        msg = "rig changed outside the broker: " + "; ".join(bad)
        if prof.transmits:
            self._fire_locked(Ev.RIG_DRIFT, reason=msg)
        elif not self._drift_warned:
            self._drift_warned = True
            self._emit("rig_drift", msg + " (non-TX profile; not a fault)", "warning")

    # ------------------------------------------------------------------ faults

    def _begin_fault_handling(self, event: Ev, reason: str, *, recover: bool) -> None:
        if self.fault is None:
            self.fault = FaultInfo(reason=reason or event.value, kind=event.value, ts=time.time())
        task = self._spawn(
            self._fault_handler(event, recover, self._switch_task, self._job_task, self._gen),
            "fault",
        )
        if recover:
            self._fault_task = task

    async def _fault_handler(self, event: Ev, recover: bool,
                             switch: asyncio.Task[None] | None,
                             job: asyncio.Task[None] | None, gen: int) -> None:
        for t in (switch, job):
            if t is not None and not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await self._inhibit_tx()
        if event is Ev.STUCK_TX and self.cfg.hooks.rig_power_cut:
            self._emit("hook", f"running rig_power_cut hook: {self.cfg.hooks.rig_power_cut}",
                       "warning")
            res = await self.jobs.run(self.cfg.hooks.rig_power_cut, timeout_s=30)
            self._emit("hook", f"rig_power_cut {'ok' if res.ok else 'FAILED'}: {res.output}",
                       "info" if res.ok else "error")
        if recover:
            await self._recover(gen)

    async def _inhibit_tx(self) -> None:
        stopped = []
        for svc in sorted(self.cfg.tx_services()):
            for attempt in range(3):
                try:
                    if await self.services.is_active(svc):
                        await self.services.stop(svc)
                        stopped.append(svc)
                    if not await self.services.is_active(svc):
                        break
                except ServiceError as exc:
                    self._emit("tx_inhibit", f"stop {svc} failed (try {attempt + 1}): {exc}",
                               "error")
                await asyncio.sleep(0.2)
            else:
                self._emit("alert", f"COULD NOT STOP TX-capable service {svc}", "alert")
        if self.consumer in self.cfg.tx_services():
            self.consumer = None
        self._emit("tx_inhibit", "TX inhibited; stopped: " + (", ".join(stopped) or "none"),
                   "warning", stopped=stopped)

    async def _recover(self, gen: int) -> None:
        s = self.safety
        now = time.monotonic()
        while self._fault_times and now - self._fault_times[0] > s.fault_window_s:
            self._fault_times.popleft()
        self._fault_times.append(now)
        if len(self._fault_times) > s.max_auto_recoveries:
            await self._fire(Ev.RECOVERY_EXHAUSTED, gen=gen,
                             reason=f"{len(self._fault_times)} faults within "
                                    f"{s.fault_window_s:g}s; manual clear required")
            return
        for attempt in range(1, s.recovery_attempts + 1):
            if self.fault:
                self.fault.recovery_attempts = attempt
            await asyncio.sleep(s.recovery_backoff_s)
            self._emit("recovery", f"recovery attempt {attempt}/{s.recovery_attempts}")
            try:
                if self.cfg.rigctld_service and s.recovery_restart_rigctld:
                    await restart(self.services, self.cfg.rigctld_service)
                hook = self.cfg.hooks.usb_power_cycle
                if hook and attempt == s.recovery_attempts and attempt > 1:
                    self._emit("hook", f"running usb_power_cycle hook: {hook}", "warning")
                    await self.jobs.run(hook, timeout_s=60)
                await self.rig.close()
                await self.rig.connect()
                await self.rig.get_freq()
                if await self.rig.get_ptt():
                    raise SwitchAbort("rig still transmitting")
                self.telemetry.online, self.telemetry.ptt = True, False
                if await self._fire(Ev.RECOVERY_OK, gen=gen,
                                    reason=f"recovered on attempt {attempt}"):
                    return
            except (RigError, ServiceError, SwitchAbort) as exc:
                self._emit("recovery", f"attempt {attempt} failed: {exc}", "warning")
        await self._fire(Ev.RECOVERY_EXHAUSTED, gen=gen,
                         reason=f"recovery failed after {s.recovery_attempts} attempts")

    # ------------------------------------------------------------------ helpers

    def _emit(self, kind: str, message: str, level: Level = "info", **data: Any) -> None:
        self.events.emit(kind, message, radio=self.id, level=level, state=self.state.value,
                         **data)

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=f"{self.id}:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._emit("internal_error", f"task {task.get_name()} crashed: {exc!r}", "error")
