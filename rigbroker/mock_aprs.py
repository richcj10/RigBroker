"""Mock APRS traffic for --mock mode: stations around you, messages, bulletins, acks.

Callsigns are obviously fake (MOCK1, MOCK2-9, ...) so nothing here can be mistaken for a
real station. Generated frames are TNC2 text and go through the same parser as real ones.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .kiss import FrameHandler

DEMO_CENTER = (37.0, -86.0)  # used only when no station position is configured
_MSG_RE = re.compile(r"^(?P<src>[^>]+)>[^:]+::(?P<to>.{9}):(?P<text>.*?)(?:\{(?P<no>\w+))?$")


def _lat(v: float) -> str:
    d = int(abs(v))
    return f"{d:02d}{(abs(v) - d) * 60:05.2f}{'N' if v >= 0 else 'S'}"


def _lon(v: float) -> str:
    d = int(abs(v))
    return f"{d:03d}{(abs(v) - d) * 60:05.2f}{'E' if v >= 0 else 'W'}"


@dataclass
class _Fake:
    call: str
    lat: float
    lon: float
    symbol: str  # table+code
    comment: str
    heading: float = 0.0
    speed: float = 0.0  # degrees per tick (tiny)
    digi: str | None = None


class MockAprsLink:
    """Implements AprsLink with simulated RF traffic."""

    def __init__(self, center: Callable[[], tuple[float, float] | None], mycall: str,
                 rng: random.Random | None = None, interval: float = 3.0) -> None:
        self.center = center
        self.mycall = mycall
        self.rng = rng or random.Random()
        self.interval = interval
        self.connected = True
        self._out: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []
        c = self.center() or DEMO_CENTER
        r = self.rng
        self.fakes = [
            _Fake("MOCK1-9", c[0] + 0.05, c[1] - 0.08, "/>", "mobile, I-65", r.uniform(0, 360),
                  0.004),
            _Fake("MOCK2-7", c[0] - 0.03, c[1] + 0.02, "/[", "walking the dog",
                  r.uniform(0, 360), 0.0008),
            _Fake("MOCK3", c[0] + 0.12, c[1] + 0.10, "/_", "WX station"),
            _Fake("MOCK4-10", c[0] - 0.09, c[1] - 0.12, "I&", "iGate + fill-in digi"),
            _Fake("MOCK5", c[0] + 0.30, c[1] - 0.25, "/#", "WIDE2 digi on the ridge"),
            _Fake("MOCK6-14", c[0] - 0.20, c[1] + 0.30, "/k", "truck", r.uniform(0, 360), 0.006,
                  digi="MOCK5"),
            _Fake("MOCK7-5", c[0] + 0.01, c[1] + 0.06, "/-", "QTH, 146.52 simplex"),
        ]

    async def run(self, on_frame: FrameHandler) -> None:
        tick = 0
        while True:
            tick += 1
            while not self._out.empty():
                await on_frame(self._out.get_nowait())
            await on_frame(self._random_frame(tick))
            await asyncio.sleep(self.interval * self.rng.uniform(0.6, 1.4))

    async def send(self, tnc2: str) -> None:
        self.sent.append(tnc2)
        m = _MSG_RE.match(tnc2)
        if not m or m.group("text").startswith(("ack", "rej")) or not m.group("no"):
            return
        to, no = m.group("to").strip(), m.group("no")
        if to.startswith("MOCK") and self.rng.random() < 0.85:  # most stations answer
            asyncio.get_running_loop().call_later(
                self.rng.uniform(2, 6), self._out.put_nowait,
                f"{to}>APRS,WIDE1-1::{self.mycall:<9}:ack{no}")

    def _random_frame(self, tick: int) -> str:
        r = self.rng
        if tick % 25 == 0:
            return (f"MOCK7-5>APK102,WIDE1-1::{self.mycall:<9}:"
                    f"Hello from MOCK7, 73 ({tick}){{{tick % 1000}")
        if tick % 40 == 0:
            return "MOCK4-10>APRS::BLN1     :Club net tonight 8pm on the repeater"
        if tick % 15 == 0:  # positionless weather report (Davis Vantage Pro style), no map dot
            return (f"MOCK8>APRS,WIDE2-1:_{time.strftime('%m%d%H%M', time.gmtime())}"
                    f"c{r.randint(0, 359):03d}s{r.randint(0, 9):03d}g{r.randint(5, 20):03d}"
                    f"t{r.randint(20, 40):03d}r000p000P000h{r.randint(80, 99):02d}"
                    f"b{r.randint(10000, 10150):05d}.DsVP")
        f = r.choice(self.fakes)
        if f.speed:
            f.heading = (f.heading + r.uniform(-20, 20)) % 360
            f.lat += f.speed * math.cos(math.radians(f.heading))
            f.lon += f.speed * math.sin(math.radians(f.heading))
        path = f"APRS,{f.digi}*,WIDE2-1" if f.digi else "APRS,WIDE1-1"
        if f.symbol == "/_":
            return (f"{f.call}>{path}:!{_lat(f.lat)}/{_lon(f.lon)}_"
                    f"{r.randint(0, 359):03d}/{r.randint(0, 15):03d}g{r.randint(5, 25):03d}"
                    f"t{r.randint(40, 85):03d}r000p000P000h{r.randint(30, 90):02d}"
                    f"b{r.randint(10050, 10250):05d}")
        if r.random() < 0.1:
            return f"{f.call}>{path}:>{f.comment}"
        crs = f"{int(f.heading):03d}/{int(f.speed * 9000):03d}" if f.speed else ""
        return (f"{f.call}>{path}:!{_lat(f.lat)}{f.symbol[0]}{_lon(f.lon)}{f.symbol[1]}"
                f"{crs}{f.comment}")
