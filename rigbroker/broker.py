"""Top level: builds one RadioController per enabled radio from config."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from . import __version__
from .aprs import AprsService
from .bbs import BbsService, MockAgwServer
from .config import Config, normalize_call
from .direwolf import SERVICE_PREFIX, DirewolfManager
from .events import EventLog
from .gps import Position
from .hwcheck import InterfaceProbe
from .jobs import JobRunner, MockJobRunner, SubprocessJobRunner
from .kiss import AprsLink, KissTcpLink
from .leases import PlanStep, Priority, Verdict
from .mock_aprs import MockAprsLink
from .mock_rig import MockRig
from .mock_sim import MockActivity
from .position import PositionSync
from .power import PowerStore
from .radio import LeaseResult, RadioController
from .rig_client import RigClient, RigctldClient
from .services import MockServiceController, ServiceController, SystemdServiceController
from .state import RadioState
from .winlink import GatewayStore, WinlinkPlanner, grid_to_latlon


class Broker:
    """Owns every radio. Interfaces (API, MQTT, CLI) talk to this object."""

    def __init__(
        self,
        config: Config,
        *,
        rigs: dict[str, RigClient],
        services: ServiceController,
        jobs: JobRunner,
        events: EventLog | None = None,
        allow_tx: bool = False,
        mock: bool = False,
        aprs_link: AprsLink | None = None,
    ) -> None:
        self.config = config
        self.events = events or EventLog()
        self.services = services
        self.jobs = jobs
        self.allow_tx = allow_tx
        self.mock = mock
        self.rigs = rigs
        self.sims: dict[str, MockActivity] = {}
        self._sim_tasks: list[asyncio.Task[None]] = []
        state_file = config.winlink.state_file
        if mock and state_file:
            state_file += ".mock"  # mock attempts must never pollute real gateway history
        self.planner = WinlinkPlanner(config.winlink, GatewayStore(state_file))
        power_file = config.power_file
        if mock and power_file:
            power_file += ".mock"  # mock choices never change the real station's power
        power_store = PowerStore(power_file)
        self.radios: dict[str, RadioController] = {
            rid: RadioController(rid, rcfg, config.safety, rigs[rid], services, jobs,
                                 self.events, allow_tx=allow_tx,
                                 attempt_listener=self._on_attempt,
                                 position_listener=self._on_position,
                                 probe=(InterfaceProbe(rcfg.interface)
                                        if rcfg.interface and not mock else None),
                                 power_store=power_store)
            for rid, rcfg in config.enabled_radios().items()
        }
        self.direwolf: DirewolfManager | None = None
        self.position_sync: PositionSync | None = None
        self._bg: set[asyncio.Task[None]] = set()
        self.aprs: AprsService | None = None
        a = config.aprs
        if a is not None:
            self.direwolf = DirewolfManager(config.direwolf, a.callsign)
            sf = config.direwolf.settings_file
            gps_file = (str(Path(os.path.expanduser(sf)).with_name("gps-last.json"))
                        + (".mock" if mock else "")) if sf else None
            self.position_sync = PositionSync(self.direwolf, gps_file, self.events,
                                              self._apply_position)
            link = aprs_link
            if link is None:
                link = (MockAprsLink(self.station_position, a.callsign) if mock
                        else KissTcpLink(a.kiss_host, a.kiss_port))
            self.aprs = AprsService(a, link, self.events, rx_gate=self.aprs_rx_gate,
                                    tx_gate=self.aprs_tx_gate, position=self.station_position)
        self.bbs: BbsService | None = None
        self._mock_agw: MockAgwServer | None = None
        b = config.bbs
        if b is not None and b.radio in self.radios and config.bbs_mycall:
            if mock:  # a pretend Direwolf + BBS, so the terminal works without RF
                self._mock_agw = MockAgwServer()
                if b.state_file:  # mock additions never touch the real station's list
                    b = b.model_copy(update={"state_file": b.state_file + ".mock"})
            agw = self._mock_agw
            self.bbs = BbsService(
                b, config.bbs_mycall, self.radios[b.radio], self.events,
                tx_allowed=lambda: self.allow_tx,
                agw_addr=(lambda: ("127.0.0.1", agw.port)) if agw is not None else None)

    # ---------------------------------------------------------------- APRS gates

    def station_position(self) -> tuple[float, float] | None:
        """The APRS radio's GPS, else the Direwolf beacon position, else the Winlink grid."""
        radio = self.radios.get(self.config.aprs.radio) if self.config.aprs else None
        if radio is not None and radio.position is not None:
            return radio.position.lat, radio.position.lon
        if self.direwolf is not None:
            s = self.direwolf.settings
            if s.lat is not None and s.lon is not None:
                return s.lat, s.lon
        if self.config.winlink.grid:
            return grid_to_latlon(self.config.winlink.grid)
        return None

    def aprs_rx_gate(self) -> tuple[bool, str]:
        """Frames are only ours while an APRS Direwolf instance owns the radio."""
        a = self.config.aprs
        r = self.radios.get(a.radio) if a else None
        if a is None or r is None:
            return False, "APRS not configured"
        if r.consumer in a.rx_services:
            return True, f"listening via {r.consumer} on {r.applied}"
        return False, f"APRS not running (radio {r.id}: {r.applied or r.state.value})"

    def aprs_tx_gate(self) -> tuple[bool, str]:
        """APRS may transmit only while the TX-capable APRS consumer is running."""
        a = self.config.aprs
        r = self.radios.get(a.radio) if a else None
        if a is None or r is None:
            return False, "APRS not configured"
        if r.fault is not None:
            return False, "radio is in FAULT (TX inhibited)"
        if r.state not in (RadioState.DEFAULT, RadioState.ACTIVE):
            return False, f"radio is {r.state.value}"
        if r.consumer != a.tx_service:
            if not self.allow_tx:
                return False, "transmit disabled (--allow-tx not set)"
            return False, (f"APRS TX needs {a.tx_service}; radio {r.id} is on "
                           f"{r.applied or '-'}")
        if not r.telemetry.online:
            return False, "rig offline"
        return True, "on the air"

    async def apply_direwolf(self) -> dict[str, Any]:
        """Write all Direwolf configs; restart the running Direwolf consumer if any."""
        assert self.direwolf is not None
        paths = [str(p) for p in self.direwolf.write()]
        self.events.emit("direwolf", f"wrote {len(paths)} Direwolf configs")
        restarted: list[str] = []
        notes: list[str] = []
        for r in self.radios.values():
            if r.consumer and r.consumer.startswith(SERVICE_PREFIX):
                ok, msg = await r.restart_consumer("Direwolf settings changed")
                (restarted if ok else notes).append(msg)
        if not restarted and not notes:
            notes.append("Direwolf not running; new settings apply on its next start")
        return {"written": paths, "restarted": restarted, "notes": notes}

    def _on_position(self, radio_id: str, pos: Position) -> None:
        """A GPS fix from a radio: the APRS radio's fixes keep Direwolf's position current."""
        aprs = self.config.aprs
        if self.position_sync is None or aprs is None or radio_id != aprs.radio:
            return
        task = asyncio.get_running_loop().create_task(self.position_sync.on_fix(pos))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _apply_position(self, restart: bool) -> None:
        """Write the Direwolf configs after a position change. The position is only used by
        the APRS beacon, so only a running direwolf@aprs is restarted - never the packet
        modem (that would cut a BBS or Winlink session)."""
        assert self.direwolf is not None
        self.direwolf.write()
        if not restart or self.config.aprs is None:
            return
        r = self.radios.get(self.config.aprs.radio)
        if r is not None and r.consumer == self.config.aprs.tx_service:
            await r.restart_consumer("APRS beacon position changed")

    def _on_attempt(self, radio_id: str, step: PlanStep, ok: bool, duration_s: float,
                    detail: str) -> None:
        self.planner.record(step.key, ok, duration_s, detail)
        self.events.emit("winlink_attempt", f"{step.label}: {'connected' if ok else detail}",
                         radio=radio_id, level="info" if ok else "warning", gateway=step.key,
                         ok=ok, duration_s=round(duration_s, 1))

    def _winlink_profile_for(self, r: RadioController, gateway: str | None,
                             profile: str | None) -> str | LeaseResult:
        """Which profile a Winlink check uses: the one asked for, else the one whose
        winlink_mode matches the chosen gateway's mode (so a VARA FM gateway starts the
        VARA FM profile tuned to it), else the radio's winlink_profile."""
        profs = r.cfg.profiles
        if profile is not None:
            if profile not in profs or not (profs[profile].winlink_mode
                                            or profile == r.cfg.winlink_profile):
                return LeaseResult(Verdict.REJECT, f"{profile!r} is not a Winlink profile",
                                   invalid=True)
            return profile
        if gateway is not None:
            try:
                call = normalize_call(gateway)
            except ValueError as exc:
                return LeaseResult(Verdict.REJECT, str(exc), invalid=True)
            gw = next((g for g in self.planner.gateways() if g.call == call), None)
            if gw is None:
                return LeaseResult(Verdict.REJECT, f"unknown gateway {call}", invalid=True)
            names = [n for n, p in profs.items() if p.winlink_mode == gw.mode]
            if not names:
                return LeaseResult(Verdict.REJECT, f"{call} is a {gw.mode} gateway but this "
                                   f"radio has no {gw.mode} profile", invalid=True)
            # the radio's main winlink_profile wins if it has the right mode
            return r.cfg.winlink_profile if r.cfg.winlink_profile in names else names[0]
        if not r.cfg.winlink_profile:
            raise LookupError(f"radio {r.id!r} has no winlink_profile configured")
        return r.cfg.winlink_profile

    async def winlink_check(self, radio_id: str, *, holder: str = "web",
                            priority: str = "manual", gateway: str | None = None,
                            profile: str | None = None) -> LeaseResult:
        """Lease a Winlink profile with a ranked gateway plan.

        The profile is ``profile``, else the one matching ``gateway``'s mode, else the
        radio's winlink_profile. Raises LookupError for an unknown radio, or for one
        without a winlink_profile when neither is given.
        """
        r = self.radios.get(radio_id)
        if r is None:
            raise LookupError(f"unknown radio {radio_id!r}")
        chosen = self._winlink_profile_for(r, gateway, profile)
        if isinstance(chosen, LeaseResult):
            return chosen
        pname = chosen
        prof = r.cfg.profiles[pname]
        if not self.allow_tx:
            return LeaseResult(Verdict.REJECT, "Winlink needs to transmit; the broker is "
                               "read-only (--allow-tx not set)")
        if not prof.winlink_mode:  # fixed-gateway profile: no planner involved
            if gateway:
                return LeaseResult(Verdict.REJECT, f"profile {pname!r} has a fixed gateway",
                                   invalid=True)
            return await r.request_lease(pname, holder=holder, priority=priority)
        try:
            plan = self.planner.plan(prof, gateway)
        except ValueError as exc:
            return LeaseResult(Verdict.REJECT, str(exc), invalid=True)
        if not plan:
            why = (f"gateway {gateway} is unknown, blocked or not usable on {pname}" if gateway
                   else f"no usable {prof.winlink_mode} gateways (add or unblock one)")
            return LeaseResult(Verdict.REJECT, why)
        self.events.emit("winlink_plan", "plan: " + " -> ".join(
            f"{s.label}@{s.freq / 1e6:.3f}" for s in plan), radio=radio_id)
        return await r.request_lease(pname, holder=holder, priority=Priority.parse(priority),
                                     plan=plan)

    @classmethod
    def build(cls, config: Config, *, mock: bool, allow_tx: bool,
              simulate: bool = True, mock_services: bool = False) -> Broker:
        """Wire dependencies.

        * ``mock``: everything simulated; never touches hardware or systemd.
        * ``mock_services``: real rigctld (e.g. Hamlib's dummy rig), simulated services/jobs.
        * otherwise: hardware mode - rigctld, systemd units, real commands.
        """
        radios = config.enabled_radios()
        if mock:
            mock_rigs: dict[str, MockRig] = {rid: MockRig() for rid in radios}
            services = MockServiceController()
            jobs = MockJobRunner(duration_s=10.0, success_rate=0.6 if simulate else None)
            quiet: AprsLink | None = None
            if not simulate and config.aprs is not None:  # tests: no random traffic
                quiet = MockAprsLink(lambda: None, config.aprs.callsign, interval=3600)
            b = cls(config, rigs=dict(mock_rigs), services=services, jobs=jobs,
                    allow_tx=allow_tx, mock=True, aprs_link=quiet)
            if simulate:
                b.sims = {rid: MockActivity(mock_rigs[rid], services, radios[rid])
                          for rid in radios}
            return b
        rigs: dict[str, RigClient] = {
            rid: RigctldClient(r.host, r.port, timeout=config.safety.rig_timeout_s)
            for rid, r in radios.items()
        }
        if mock_services:  # real rigctld (e.g. Hamlib dummy), simulated services/jobs
            return cls(config, rigs=rigs, services=MockServiceController(),
                       jobs=MockJobRunner(duration_s=10.0), allow_tx=allow_tx)
        allowed: set[str] = set()
        for r in radios.values():
            allowed |= r.all_services()
            if r.rigctld_service:
                allowed.add(r.rigctld_service)
        return cls(config, rigs=rigs, services=SystemdServiceController(allowed),
                   jobs=SubprocessJobRunner(), allow_tx=allow_tx)

    def mock_rig(self, radio_id: str) -> MockRig:
        """The MockRig behind a radio (mock mode only)."""
        rig = self.rigs.get(radio_id)
        if not self.mock or not isinstance(rig, MockRig):
            raise LookupError("not in mock mode")
        return rig

    def inject(self, radio_id: str, what: str) -> str:
        """Mock-mode fault injection (used by the UI's mock panel and tests)."""
        sim = self.sims.get(radio_id)
        if sim is None:
            sim = MockActivity(self.mock_rig(radio_id), MockServiceController(),
                               self.config.radios[radio_id])
        msg = sim.inject(what)
        self.events.emit("mock_inject", f"injected {what}: {msg}", radio=radio_id,
                         level="warning")
        return msg

    async def start(self) -> None:
        mode = ("MOCK" if self.mock else
                "HARDWARE (simulated services)" if isinstance(self.services, MockServiceController)
                else "HARDWARE")
        self.events.emit("broker", f"RigBroker {__version__} starting ({mode}, "
                         f"allow_tx={self.allow_tx}, radios: {', '.join(self.radios)})")
        if self.position_sync is not None:
            await self.position_sync.startup()  # no position yet: last known GPS fix
        if self.direwolf is not None:
            # Direwolf must find a current config whenever the broker starts it.
            self.direwolf.write()
            where = self.direwolf.installed() or "NOT INSTALLED"
            self.events.emit("direwolf", f"Direwolf configs written to "
                             f"{self.direwolf.conf_dir} (direwolf: {where})")
        for r in self.radios.values():
            await r.start()
        self._sim_tasks = [asyncio.create_task(s.run(), name=f"sim-{rid}")
                           for rid, s in self.sims.items()]
        if self.aprs is not None:
            await self.aprs.start()
        if self._mock_agw is not None:
            await self._mock_agw.start()

    async def stop(self) -> None:
        if self.bbs is not None:
            await self.bbs.stop()
        if self._mock_agw is not None:
            await self._mock_agw.stop()
        if self.aprs is not None:
            await self.aprs.stop()
        for t in self._sim_tasks:
            t.cancel()
        for t in self._sim_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await asyncio.gather(*(r.stop() for r in self.radios.values()), return_exceptions=True)
        self.events.emit("broker", "RigBroker stopped")

    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "mock": self.mock,
            "allow_tx": self.allow_tx,
            "radios": {rid: r.status() for rid, r in self.radios.items()},
            "disabled_radios": sorted(set(self.config.radios) - set(self.radios)),
            "schedule": [],  # scheduler arrives in milestone 5
            "aprs": self.aprs.status() if self.aprs else None,
            "bbs": self.bbs.status() if self.bbs else None,
            "gps": self.position_sync.status() if self.position_sync else None,
            "winlink": {"grid": self.config.winlink.grid,
                        "period": self.planner.day_or_night(),
                        "max_attempts": self.config.winlink.max_attempts},
        }
