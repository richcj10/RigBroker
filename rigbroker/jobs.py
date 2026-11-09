"""Run commands (profile ``run`` jobs such as ``pat connect``, and fault hooks)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import shlex
import signal
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Behavior = Literal["success", "fail", "hang"]


@dataclass(frozen=True)
class JobResult:
    """Outcome of one command run."""

    ok: bool
    returncode: int | None
    output: str
    timed_out: bool
    duration_s: float


@runtime_checkable
class JobRunner(Protocol):
    """Runs a command with a timeout; must kill it on timeout or cancellation."""

    async def run(self, cmd: str, timeout_s: float) -> JobResult: ...


class SubprocessJobRunner:
    """Runs real commands, safely.

    * **No shell**: the command is split with shlex and exec'd directly, so nothing in it
      (e.g. a gateway callsign) can inject shell syntax.
    * Own process group; on timeout or cancellation the whole group gets SIGTERM, then
      SIGKILL after ``grace_s`` (lets Pat close its AX.25 link cleanly first).
    * Output is captured (stdout+stderr) and truncated to ``max_output`` characters.
    """

    def __init__(self, *, grace_s: float = 5.0, max_output: int = 20000) -> None:
        self.grace_s = grace_s
        self.max_output = max_output

    async def run(self, cmd: str, timeout_s: float) -> JobResult:
        t0 = time.monotonic()
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            return JobResult(False, None, f"bad command: {exc}", False, 0.0)
        if not argv:
            return JobResult(False, None, "empty command", False, 0.0)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return JobResult(False, None, f"cannot start {argv[0]}: {exc}", False,
                             time.monotonic() - t0)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            await self._terminate(proc)
            return JobResult(False, proc.returncode, f"timed out after {timeout_s:.0f}s; "
                             "process group terminated", True, time.monotonic() - t0)
        except asyncio.CancelledError:
            await self._terminate(proc)
            raise
        text = out.decode(errors="replace")[-self.max_output:]
        return JobResult(proc.returncode == 0, proc.returncode, text, False,
                         time.monotonic() - t0)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        for sig, wait in ((signal.SIGTERM, self.grace_s), (signal.SIGKILL, 5.0)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, sig)
            try:
                await asyncio.wait_for(proc.wait(), wait)
                return
            except TimeoutError:
                continue


@dataclass
class MockJobRunner:
    """Simulated runner. Never executes anything.

    Outcome per run, in priority order:
      * ``script``: consume the next behavior from this list (tests)
      * ``success_rate``: random success with this probability (mock demo)
      * ``behavior``: fixed success / fail / hang
    """

    behavior: Behavior = "success"
    duration_s: float = 0.05
    script: list[Behavior] = field(default_factory=list)
    success_rate: float | None = None
    rng: random.Random = field(default_factory=random.Random)
    runs: list[str] = field(default_factory=list)

    def _next(self) -> Behavior:
        if self.script:
            return self.script.pop(0)
        if self.success_rate is not None:
            return "success" if self.rng.random() < self.success_rate else "fail"
        return self.behavior

    async def run(self, cmd: str, timeout_s: float) -> JobResult:
        self.runs.append(cmd)
        behavior = self._next()
        t0 = time.monotonic()
        if behavior == "hang":
            await asyncio.sleep(timeout_s)
            return JobResult(False, None, "mock: hung, killed", True, time.monotonic() - t0)
        await asyncio.sleep(min(self.duration_s, timeout_s))
        if behavior == "fail":
            return JobResult(False, 1, "mock: no answer from gateway", False,
                             time.monotonic() - t0)
        return JobResult(True, 0, "mock: connected, 0 messages exchanged", False,
                         time.monotonic() - t0)
