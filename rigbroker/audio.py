"""Receive audio to the browser: relays raw PCM from audio-stream.service to listeners.

audio-stream.service (run as rigbroker, which may open the sound card - the broker itself runs
with PrivateDevices and cannot) captures the radio's RX audio as 16 kHz mono signed 16-bit
little-endian and offers it on a Unix socket. While that service is the radio's consumer, the
relay reads it in 100 ms chunks and fans them out to WebSocket listeners. A slow listener only
loses chunks; it never stalls the relay. Receive only: nothing here can key a radio.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import struct
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("rigbroker.audio")

RATE = 16000  # Hz, mono, S16LE
CHUNK = RATE // 10 * 2  # 100 ms of audio in bytes
BACKLOG_S = 0.3  # audio received this soon after connecting is stale (pipe buffer): dropped
SERVICE = "audio-stream"


class AudioRelay:
    """Fan-out of the RX audio stream. ``active()`` says when the source should be running."""

    def __init__(self, socket_path: str, active: Callable[[], bool], *,
                 source: Callable[[], Awaitable[tuple[asyncio.StreamReader, object]]]
                 | None = None) -> None:
        self.socket_path = socket_path
        self.active = active
        self._source = source
        self._subs: set[asyncio.Queue[bytes]] = set()
        self._task: asyncio.Task[None] | None = None
        self.connected = False
        self.chunks = 0

    # ------------------------------------------------------------ listeners

    def subscribe(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)  # ~2 s, then drop
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subs.discard(q)

    @property
    def listeners(self) -> int:
        return len(self._subs)

    def publish(self, chunk: bytes) -> None:
        self.chunks += 1
        for q in list(self._subs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(chunk)

    # ------------------------------------------------------------ source

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="audio-relay")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _open(self) -> tuple[asyncio.StreamReader, object]:
        if self._source is not None:
            return await self._source()
        r, w = await asyncio.open_unix_connection(self.socket_path)
        return r, w

    async def _run(self) -> None:
        while True:
            if not self.active():
                self.connected = False
                await asyncio.sleep(0.5)
                continue
            try:
                reader, writer = await self._open()
            except OSError:
                await asyncio.sleep(1.0)  # service still starting (or restarting)
                continue
            self.connected = True
            log.info("audio source connected")
            try:
                await self._pump(reader)
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                self.connected = False
                close = getattr(writer, "close", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        close()
            await asyncio.sleep(0.2)

    async def _pump(self, reader: asyncio.StreamReader) -> None:
        stale_until = time.monotonic() + BACKLOG_S
        buf = bytearray()
        while self.active():
            data = await asyncio.wait_for(reader.read(8192), 2.0)
            if not data:
                return  # service stopped / restarted
            if time.monotonic() < stale_until:
                continue  # pipe backlog from before we connected
            buf += data
            while len(buf) >= CHUNK:
                self.publish(bytes(buf[:CHUNK]))
                del buf[:CHUNK]


class _Closer:
    """Stops a mock feeder when the relay closes its connection."""

    def __init__(self, task: asyncio.Task[None]) -> None:
        self.task = task

    def close(self) -> None:
        self.task.cancel()


class MockAudio:
    """A stand-in for audio-stream in --mock: soft noise, and 'voice' (warbling tones) while
    the mock radio's squelch is open, so the Airband tab can be tried without RF."""

    def __init__(self, squelch_open: Callable[[], bool]) -> None:
        self.squelch_open = squelch_open
        self.rng = random.Random(4100)
        self._phase = 0.0

    async def open(self) -> tuple[asyncio.StreamReader, object]:
        reader = asyncio.StreamReader()
        task = asyncio.get_running_loop().create_task(self._feed(reader))
        return reader, _Closer(task)

    async def _feed(self, reader: asyncio.StreamReader) -> None:
        n = CHUNK // 2
        while True:
            t0 = time.monotonic()
            voice = self.squelch_open()
            samples = []
            for i in range(n):
                if voice:
                    f = 300 + 200 * math.sin(2 * math.pi * 3 * (self._phase + i / RATE))
                    v = 6000 * math.sin(2 * math.pi * f * (self._phase + i / RATE))
                else:
                    v = self.rng.gauss(0, 120)
                samples.append(int(max(-32767, min(32767, v))))
            self._phase += n / RATE
            reader.feed_data(struct.pack(f"<{n}h", *samples))
            await asyncio.sleep(max(0.0, 0.1 - (time.monotonic() - t0)))


__all__ = ["CHUNK", "RATE", "SERVICE", "AudioRelay", "MockAudio"]
