"""A fake rigctld: asyncio TCP server speaking enough of the rigctld protocol.

Backed by a MockRig, so the MockRig's fault injection (readback_override, fail_commands,
offline, ptt/dcd) applies. Extra transport-level faults:
  - ``silent = True``: read commands but never answer (client times out)
  - ``drop_next = True``: close the connection on the next command

Run standalone for manual testing:  python -m tests.fake_rigctld --port 4542
"""

from __future__ import annotations

import argparse
import asyncio

from rigbroker.mock_rig import MockRig
from rigbroker.rig_client import RigError

SHORT = {"f": "get_freq", "F": "set_freq", "m": "get_mode", "M": "set_mode", "t": "get_ptt",
         "l": "get_level", "L": "set_level", "_": "get_info"}


class FakeRigctld:
    def __init__(self, rig: MockRig | None = None) -> None:
        self.rig = rig or MockRig()
        self.silent = False
        self.drop_next = False
        self.connections = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for w in list(self._writers):
            w.close()
        if self._server:
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        try:
            while line := await reader.readline():
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                if self.drop_next:
                    self.drop_next = False
                    return
                if self.silent:
                    continue
                writer.write((await self.dispatch(text)).encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def dispatch(self, text: str) -> str:
        """Handle one command line; returns the full reply text."""
        ext = text.startswith("+")
        parts = text.lstrip("+").split()
        name = SHORT.get(parts[0], parts[0].lstrip("\\"))
        args = parts[1:]
        header = name + ":" + "".join(" " + a for a in args) + "\n"
        try:
            vals = await self._call(name, args)
        except RigError as exc:
            code = exc.code if exc.code is not None else -5
            return (header if ext else "") + f"RPRT {code}\n"
        except (ValueError, IndexError):
            return (header if ext else "") + "RPRT -1\n"
        if ext:
            return header + "".join(v + "\n" for v in vals) + "RPRT 0\n"
        if name.startswith("set_"):
            return "RPRT 0\n"
        return "".join((v.partition(": ")[2] or v) + "\n" for v in vals)

    async def _call(self, name: str, a: list[str]) -> list[str]:
        r = self.rig
        if name == "get_freq":
            return [f"Frequency: {await r.get_freq()}"]
        if name == "set_freq":
            await r.set_freq(round(float(a[0])))
            return []
        if name == "get_mode":
            mode, pb = await r.get_mode()
            return [f"Mode: {mode}", f"Passband: {pb}"]
        if name == "set_mode":
            await r.set_mode(a[0], int(a[1]) if len(a) > 1 else 0)
            return []
        if name == "get_ptt":
            return [f"PTT: {int(await r.get_ptt())}"]
        if name == "get_dcd":
            return [f"DCD: {int(await r.get_dcd())}"]
        if name == "get_level":
            return [f"{await r.get_level(a[0]):g}"]
        if name == "set_level":
            await r.set_level(a[0], float(a[1]))
            return []
        if name == "get_powerstat":
            return [f"Power Status: {int(await r.get_powerstat())}"]
        if name == "set_powerstat":
            await r.set_powerstat(a[0] != "0")
            return []
        if name == "get_info":
            return ["Info: FakeRigctld"]
        raise RigError(f"{name}: not implemented", -4)


async def _main(port: int) -> None:
    srv = FakeRigctld()
    await srv.start(port=port)
    print(f"fake rigctld on 127.0.0.1:{srv.port}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=4542)
    asyncio.run(_main(p.parse_args().port))
