"""Rig control interface and the rigctld TCP implementation.

Uses rigctld's *extended response* protocol (commands prefixed with ``+``) so every
reply is terminated by an ``RPRT n`` line; non-zero ``n`` raises :class:`RigError`.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol, runtime_checkable

LEVELS = ("RFPOWER", "SQL", "AF", "STRENGTH")


class RigError(Exception):
    """A rig command failed (rigctld returned RPRT -n, or the link is down)."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class RigTimeout(RigError):
    """No reply within the timeout."""


class RigConnectionError(RigError):
    """Cannot reach rigctld / connection dropped."""


@runtime_checkable
class RigClient(Protocol):
    """Async rig control. Every method raises RigError on failure."""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def get_freq(self) -> int: ...
    async def set_freq(self, hz: int) -> None: ...
    async def get_mode(self) -> tuple[str, int]: ...
    async def set_mode(self, mode: str, passband: int = 0) -> None: ...
    async def get_ptt(self) -> bool: ...
    async def get_dcd(self) -> bool: ...
    async def get_level(self, name: str) -> float: ...
    async def set_level(self, name: str, value: float) -> None: ...
    async def get_powerstat(self) -> bool: ...
    async def set_powerstat(self, on: bool) -> None: ...
    async def get_info(self) -> str: ...


def _value(line: str) -> str:
    """'Frequency: 144390000' -> '144390000'; '0.5' -> '0.5'."""
    _, sep, val = line.partition(": ")
    return val.strip() if sep else line.strip()


class RigctldClient:
    """RigClient speaking to Hamlib rigctld over TCP.

    One TCP connection, one command in flight (guarded by a lock). Reconnects lazily on
    the next command after a timeout or disconnect.
    """

    def __init__(self, host: str, port: int, timeout: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def connect(self) -> None:
        """(Re)connect to rigctld."""
        async with self._lock:
            await self._connect_locked()

    async def close(self) -> None:
        """Close the connection (safe to call repeatedly)."""
        async with self._lock:
            await self._close_locked()

    async def _connect_locked(self) -> None:
        await self._close_locked()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except (OSError, TimeoutError) as exc:
            raise RigConnectionError(
                f"cannot connect to rigctld {self.host}:{self.port}: {exc or 'timeout'}"
            ) from exc

    async def _close_locked(self) -> None:
        w, self._writer, self._reader = self._writer, None, None
        if w is not None:
            w.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(w.wait_closed(), 1.0)

    async def _cmd(self, cmd: str, *args: str) -> list[str]:
        line = "+\\" + cmd + "".join(" " + a for a in args)
        async with self._lock:
            if self._writer is None:
                await self._connect_locked()
            try:
                return await asyncio.wait_for(self._exchange(cmd, line), self.timeout)
            except TimeoutError:
                await self._close_locked()  # stream may be out of sync
                raise RigTimeout(f"{cmd}: no reply from rigctld in {self.timeout}s") from None
            except (OSError, ConnectionError) as exc:
                await self._close_locked()
                raise RigConnectionError(f"{cmd}: rigctld connection lost: {exc}") from exc

    async def _exchange(self, cmd: str, line: str) -> list[str]:
        assert self._reader is not None and self._writer is not None
        self._writer.write((line + "\n").encode())
        await self._writer.drain()
        lines: list[str] = []
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise ConnectionError("rigctld closed the connection")
            text = raw.decode(errors="replace").rstrip("\r\n")
            if text.startswith("RPRT "):
                try:
                    code = int(text[5:].strip())
                except ValueError:
                    raise RigError(f"{cmd}: malformed reply {text!r}") from None
                if code != 0:
                    raise RigError(f"{cmd} failed: RPRT {code}", code)
                break
            lines.append(text)
        if lines and lines[0].startswith(cmd):
            lines = lines[1:]  # echoed command header
        return lines

    async def _get(self, cmd: str, *args: str, n: int = 1) -> list[str]:
        lines = await self._cmd(cmd, *args)
        if len(lines) < n:
            raise RigError(f"{cmd}: short reply {lines!r}")
        return [_value(x) for x in lines[:n]]

    async def get_freq(self) -> int:
        (v,) = await self._get("get_freq")
        return round(float(v))

    async def set_freq(self, hz: int) -> None:
        await self._cmd("set_freq", str(int(hz)))

    async def get_mode(self) -> tuple[str, int]:
        mode, pb = await self._get("get_mode", n=2)
        return mode.upper(), int(float(pb))

    async def set_mode(self, mode: str, passband: int = 0) -> None:
        await self._cmd("set_mode", mode.upper(), str(int(passband)))

    async def get_ptt(self) -> bool:
        (v,) = await self._get("get_ptt")
        return v not in ("0", "")

    async def get_dcd(self) -> bool:
        (v,) = await self._get("get_dcd")
        return v not in ("0", "")

    async def get_level(self, name: str) -> float:
        (v,) = await self._get("get_level", name.upper())
        return float(v)

    async def set_level(self, name: str, value: float) -> None:
        await self._cmd("set_level", name.upper(), f"{value:.6g}")

    async def get_powerstat(self) -> bool:
        (v,) = await self._get("get_powerstat")
        return v not in ("0", "")

    async def set_powerstat(self, on: bool) -> None:
        await self._cmd("set_powerstat", "1" if on else "0")

    async def get_info(self) -> str:
        lines = await self._cmd("get_info")
        return " ".join(_value(x) for x in lines)
