"""Audio-consumer service control (systemd in production, in-memory mock for tests)."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Protocol, runtime_checkable


class ServiceError(Exception):
    """Starting/stopping a service failed."""


@runtime_checkable
class ServiceController(Protocol):
    """Starts/stops named services (e.g. ``direwolf@aprs``)."""

    async def start(self, name: str) -> None: ...
    async def stop(self, name: str) -> None: ...
    async def is_active(self, name: str) -> bool: ...
    async def exists(self, name: str) -> bool: ...


async def restart(ctrl: ServiceController, name: str) -> None:
    """Stop then start a service."""
    await ctrl.stop(name)
    await ctrl.start(name)


_UNIT_RE = re.compile(r"^[A-Za-z0-9@_.\-]{1,100}$")


class SystemdServiceController:
    """ServiceController backed by ``systemctl`` (system units).

    Only units in ``allowed`` (RigBroker's own, from the config) can ever be touched; the
    OS side is enforced again by the polkit rule (deploy/50-rigbroker.rules), which lets
    the ``rigbroker`` user start/stop/restart exactly those units and nothing else.
    """

    def __init__(self, allowed: set[str], *, systemctl: str = "systemctl",
                 timeout_s: float = 60.0) -> None:
        self.allowed = set(allowed)
        self.systemctl = systemctl
        self.timeout_s = timeout_s

    def _unit(self, name: str) -> str:
        if name not in self.allowed or not _UNIT_RE.match(name):
            raise ServiceError(f"{name!r} is not a RigBroker-managed unit")
        return name if name.endswith((".service", ".target")) else f"{name}.service"

    async def _run(self, *args: str) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.systemctl, "--no-ask-password", *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as exc:
            raise ServiceError(f"cannot run {self.systemctl}: {exc}") from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise ServiceError(f"systemctl {' '.join(args)} timed out") from None
        return proc.returncode or 0, out.decode(errors="replace").strip()

    async def _change(self, verb: str, name: str) -> None:
        unit = self._unit(name)
        rc, out = await self._run(verb, unit)
        if rc != 0:
            raise ServiceError(f"systemctl {verb} {unit} failed (rc {rc}): {out[-300:]}")

    async def start(self, name: str) -> None:
        await self._change("start", name)

    async def stop(self, name: str) -> None:
        await self._change("stop", name)

    async def is_active(self, name: str) -> bool:
        rc, _ = await self._run("is-active", "--quiet", self._unit(name))
        return rc == 0

    async def exists(self, name: str) -> bool:
        """True if the unit file is installed (LoadState=loaded)."""
        rc, out = await self._run("show", "-p", "LoadState", "--value", self._unit(name))
        return rc == 0 and out.strip() == "loaded"


class MockServiceController:
    """In-memory ServiceController. Never touches systemd.

    Fault injection: add names to ``fail_start`` / ``fail_stop``.
    ``history`` records ("start"|"stop", name) in order.
    """

    def __init__(self) -> None:
        self.active: set[str] = set()
        self.fail_start: set[str] = set()
        self.fail_stop: set[str] = set()
        self.missing: set[str] = set()  # units reported as not installed
        self.history: list[tuple[str, str]] = []

    def started(self) -> list[str]:
        """Names that were ever started, in order."""
        return [n for op, n in self.history if op == "start"]

    async def start(self, name: str) -> None:
        self.history.append(("start", name))
        if name in self.fail_start:
            raise ServiceError(f"mock: failed to start {name}")
        self.active.add(name)

    async def stop(self, name: str) -> None:
        self.history.append(("stop", name))
        if name in self.fail_stop:
            raise ServiceError(f"mock: failed to stop {name}")
        self.active.discard(name)

    async def is_active(self, name: str) -> bool:
        return name in self.active

    async def exists(self, name: str) -> bool:
        return name not in self.missing
