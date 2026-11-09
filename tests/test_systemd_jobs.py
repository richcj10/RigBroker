"""Real (non-mock) ServiceController and JobRunner."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from rigbroker.jobs import SubprocessJobRunner
from rigbroker.services import ServiceError, SystemdServiceController

posix = pytest.mark.skipif(os.name != "posix", reason="POSIX only")

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$@" >> "$RB_LOG"
verb=$2
case "$verb" in
  start) if [ "$RB_FAIL" = "$3" ]; then echo "Job for $3: control process exited"; exit 1; fi
         touch "$RB_STATE/$3" ;;
  stop) rm -f "$RB_STATE/$3" ;;
  is-active) if [ -f "$RB_STATE/$4" ]; then exit 0; else exit 3; fi ;;
  show) case "$6" in vara-fm.service) echo not-found ;; *) echo loaded ;; esac ;;
esac
"""


@pytest.fixture
def fake_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    exe = tmp_path / "systemctl"
    exe.write_text(FAKE_SYSTEMCTL)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "log"
    monkeypatch.setenv("RB_LOG", str(log))
    monkeypatch.setenv("RB_STATE", str(state))
    monkeypatch.delenv("RB_FAIL", raising=False)
    return exe, log


@posix
async def test_systemd_roundtrip(fake_systemctl: tuple[Path, Path]) -> None:
    exe, log = fake_systemctl
    s = SystemdServiceController({"direwolf@aprs", "rigctld-vu"}, systemctl=str(exe))
    assert not await s.is_active("direwolf@aprs")
    await s.start("direwolf@aprs")
    assert await s.is_active("direwolf@aprs")
    await s.stop("direwolf@aprs")
    assert not await s.is_active("direwolf@aprs")
    lines = log.read_text().splitlines()
    assert "--no-ask-password start direwolf@aprs.service" in lines
    assert "--no-ask-password stop direwolf@aprs.service" in lines


@posix
async def test_systemd_exists(fake_systemctl: tuple[Path, Path]) -> None:
    exe, log = fake_systemctl
    s = SystemdServiceController({"direwolf@aprs", "vara-fm"}, systemctl=str(exe))
    assert await s.exists("direwolf@aprs") is True
    assert await s.exists("vara-fm") is False
    assert "--no-ask-password show -p LoadState --value vara-fm.service" in log.read_text()


@posix
async def test_systemd_allowlist(fake_systemctl: tuple[Path, Path]) -> None:
    exe, log = fake_systemctl
    s = SystemdServiceController({"direwolf@aprs"}, systemctl=str(exe))
    for bad in ("ssh", "nftables.service", "direwolf@aprs; reboot", "../x"):
        with pytest.raises(ServiceError, match="not a RigBroker-managed unit"):
            await s.start(bad)
    assert not log.exists()  # systemctl was never even run


@posix
async def test_systemd_failure_message(fake_systemctl: tuple[Path, Path],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    exe, _ = fake_systemctl
    monkeypatch.setenv("RB_FAIL", "vara-fm.service")
    s = SystemdServiceController({"vara-fm"}, systemctl=str(exe))
    with pytest.raises(ServiceError, match="control process exited"):
        await s.start("vara-fm")


async def test_systemd_missing_binary() -> None:
    s = SystemdServiceController({"x"}, systemctl="/nonexistent/systemctl")
    with pytest.raises(ServiceError, match="cannot run"):
        await s.is_active("x")


@pytest.mark.skipif(not Path("/run/systemd/system").exists(), reason="needs systemd")
async def test_real_systemctl_is_active_is_read_only() -> None:
    # query only; never starts/stops anything
    s = SystemdServiceController({"rigbroker-test-nonexistent"})
    assert await s.is_active("rigbroker-test-nonexistent") is False


# ---------------------------------------------------------------- jobs


@posix
async def test_job_success_and_failure() -> None:
    r = SubprocessJobRunner()
    ok = await r.run("echo hello from pat", 5)
    assert ok.ok and ok.returncode == 0 and "hello from pat" in ok.output
    bad = await r.run("sh -c 'echo nope; exit 3'", 5)
    assert not bad.ok and bad.returncode == 3 and "nope" in bad.output
    missing = await r.run("/nonexistent/pat connect", 5)
    assert not missing.ok and "cannot start" in missing.output
    assert not (await r.run("echo 'unterminated", 5)).ok


@posix
async def test_job_no_shell_injection(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    res = await SubprocessJobRunner().run(f"echo hi; touch {marker}", 5)
    assert res.ok and "hi; touch" in res.output  # just arguments to echo
    assert not marker.exists()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@posix
async def test_job_timeout_kills_whole_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    t0 = time.monotonic()
    res = await SubprocessJobRunner(grace_s=1).run(
        f"sh -c 'sleep 30 & echo $! > {pidfile}; wait'", 0.5)
    assert res.timed_out and not res.ok and time.monotonic() - t0 < 5
    child = int(pidfile.read_text())
    for _ in range(50):
        if not _alive(child):
            break
        await asyncio.sleep(0.05)
    assert not _alive(child), "grandchild survived the timeout"


@posix
async def test_job_cancel_kills_process(tmp_path: Path) -> None:
    pidfile = tmp_path / "p.pid"
    task = asyncio.create_task(SubprocessJobRunner(grace_s=1).run(
        f"sh -c 'echo $$ > {pidfile}; exec sleep 30'", 60))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.02)
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(pid)


def test_shutil_has_sh() -> None:
    assert os.name != "posix" or shutil.which("sh")
