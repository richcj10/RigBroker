"""Winlink gateway planner: favorites, blocklist, attempt history and ranking.

Ranking (higher score first; every gateway carries the reasons behind its score):
  * favorite                         +100
  * measured success rate            up to +60  (Laplace-smoothed: (ok+1)/(tries+2))
  * last two attempts both failed    -30
  * distance from your grid          -1 per 10 km (capped at -50)
  * HF band preferred for day/night  +20  (sun elevation at your grid)

Only gateways whose mode matches the profile's ``winlink_mode`` and whose frequency lies in
the profile's ``freq_ranges`` are usable; blocked gateways are never planned.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .config import (
    GatewayConfig,
    Profile,
    WinlinkConfig,
    WinlinkMode,
    normalize_call,
    normalize_grid,
)
from .leases import PlanStep

log = logging.getLogger("rigbroker.winlink")

_BANDS = (
    (1_800_000, 2_000_000, "160m"), (3_500_000, 4_000_000, "80m"), (5_330_000, 5_410_000, "60m"),
    (7_000_000, 7_300_000, "40m"), (10_100_000, 10_150_000, "30m"),
    (14_000_000, 14_350_000, "20m"), (18_068_000, 18_168_000, "17m"),
    (21_000_000, 21_450_000, "15m"), (24_890_000, 24_990_000, "12m"),
    (28_000_000, 29_700_000, "10m"), (50_000_000, 54_000_000, "6m"),
    (144_000_000, 148_000_000, "2m"), (222_000_000, 225_000_000, "1.25m"),
    (420_000_000, 450_000_000, "70cm"),
)
HF_LIMIT_HZ = 30_000_000
HISTORY_LEN = 10


def band_of(hz: int) -> str | None:
    """Amateur band name for a frequency, or None."""
    for lo, hi, name in _BANDS:
        if lo <= hz <= hi:
            return name
    return None


def grid_to_latlon(grid: str) -> tuple[float, float]:
    """Centre of a 4- or 6-character Maidenhead square as (lat, lon) degrees."""
    g = grid.strip()
    lon = float((ord(g[0].upper()) - 65) * 20 - 180 + int(g[2]) * 2)
    lat = float((ord(g[1].upper()) - 65) * 10 - 90 + int(g[3]))
    if len(g) >= 6:
        lon += (ord(g[4].lower()) - 97) * (5 / 60) + 2.5 / 60
        lat += (ord(g[5].lower()) - 97) * (2.5 / 60) + 1.25 / 60
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon


def distance_km(a: str, b: str) -> float:
    """Great-circle distance between two grid squares."""
    (la1, lo1), (la2, lo2) = grid_to_latlon(a), grid_to_latlon(b)
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))


def sun_elevation(lat: float, lon: float, when: datetime) -> float:
    """Approximate solar elevation in degrees (good to ~1 degree; enough for day/night)."""
    when = when.astimezone(UTC)
    doy = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60 + when.second / 3600
    b = math.radians(360 / 365 * (doy - 81))
    decl = math.radians(23.44 * math.sin(b))
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)  # minutes
    solar_min = hours * 60 + eot + 4 * lon
    ha = math.radians(solar_min / 4 - 180)
    la = math.radians(lat)
    s = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


@dataclass
class Gateway:
    call: str
    freq: int
    mode: WinlinkMode
    grid: str | None = None
    note: str = ""
    source: str = "config"  # config | user


@dataclass
class GatewayStats:
    attempts: int = 0
    successes: int = 0
    recent: list[bool] = field(default_factory=list)  # newest last
    last_ok: float | None = None
    last_try: float | None = None
    last_detail: str = ""

    def rate(self) -> float:
        return (self.successes + 1) / (self.attempts + 2)


@dataclass
class Ranked:
    gateway: Gateway
    score: float
    reasons: list[str]
    favorite: bool
    blocked: bool
    usable: bool
    distance_km: float | None
    stats: GatewayStats

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self.gateway)
        d.update(score=round(self.score, 1), reasons=self.reasons, favorite=self.favorite,
                 blocked=self.blocked, usable=self.usable, band=band_of(self.gateway.freq),
                 distance_km=None if self.distance_km is None else round(self.distance_km),
                 stats=asdict(self.stats))
        return d


class GatewayStore:
    """Persistent favorites / blocklist / user gateways / history (JSON, atomic writes).

    ``path=None`` keeps everything in memory (tests, or if you don't want history saved).
    """

    def __init__(self, path: str | None) -> None:
        self.path = Path(os.path.expanduser(path)) if path else None
        self.favorites: dict[str, bool] = {}  # explicit overrides of the config flag
        self.blocked: set[str] = set()
        self.user_gateways: dict[str, Gateway] = {}
        self.stats: dict[str, GatewayStats] = {}
        self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.favorites = {k: bool(v) for k, v in data.get("favorites", {}).items()}
            self.blocked = set(data.get("blocked", []))
            self.user_gateways = {k: Gateway(**v) for k, v in data.get("user_gateways", {}).items()}
            self.stats = {k: GatewayStats(**v) for k, v in data.get("stats", {}).items()}
        except (OSError, ValueError, TypeError) as exc:
            bad = self.path.with_suffix(f".corrupt-{int(time.time())}")
            log.error("winlink state %s unreadable (%s); moved to %s, starting fresh",
                      self.path, exc, bad)
            self.path.replace(bad)

    def save(self) -> None:
        if self.path is None:
            return
        data = {
            "favorites": self.favorites,
            "blocked": sorted(self.blocked),
            "user_gateways": {k: asdict(v) for k, v in self.user_gateways.items()},
            "stats": {k: asdict(v) for k, v in self.stats.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".winlink-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)


class WinlinkPlanner:
    """Ranks gateways and builds lease plans."""

    def __init__(self, cfg: WinlinkConfig, store: GatewayStore,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.cfg = cfg
        self.store = store
        self.now = now
        self._config_favorite = {g.call: g.favorite for g in cfg.gateways}

    # ---------------------------------------------------------------- gateways

    def gateways(self) -> list[Gateway]:
        """Config gateways plus user-added ones (a user entry overrides the same call)."""
        out = {g.call: Gateway(g.call, g.freq, g.mode, g.grid, g.note, "config")
               for g in self.cfg.gateways}
        out.update(self.store.user_gateways)
        return list(out.values())

    def is_favorite(self, call: str) -> bool:
        return self.store.favorites.get(call, self._config_favorite.get(call, False))

    def set_favorite(self, call: str, favorite: bool) -> None:
        call = self._known(call)
        self.store.favorites[call] = favorite
        self.store.save()

    def set_blocked(self, call: str, blocked: bool) -> None:
        call = self._known(call)
        if blocked:
            self.store.blocked.add(call)
        else:
            self.store.blocked.discard(call)
        self.store.save()

    def add_gateway(self, call: str, freq: int, mode: WinlinkMode, grid: str | None = None,
                    note: str = "", favorite: bool = True) -> Gateway:
        """Add (or replace) a user gateway; validated like config entries. Raises ValueError."""
        g = GatewayConfig(call=call, freq=freq, mode=mode, grid=grid, note=note,
                          favorite=favorite)
        gw = Gateway(g.call, g.freq, g.mode, g.grid, g.note, "user")
        self.store.user_gateways[gw.call] = gw
        self.store.favorites[gw.call] = favorite
        self.store.save()
        return gw

    def remove_gateway(self, call: str) -> None:
        """Remove a user-added gateway (config gateways can only be blocked)."""
        call = normalize_call(call)
        if call not in self.store.user_gateways:
            raise KeyError(f"{call} is not a user-added gateway")
        del self.store.user_gateways[call]
        self.store.favorites.pop(call, None)
        self.store.save()

    def record(self, call: str, ok: bool, duration_s: float, detail: str = "") -> None:
        """Remember the outcome of one connection attempt."""
        st = self.store.stats.setdefault(call, GatewayStats())
        st.attempts += 1
        st.successes += int(ok)
        st.recent = [*st.recent, ok][-HISTORY_LEN:]
        st.last_try = time.time()
        if ok:
            st.last_ok = st.last_try
        st.last_detail = f"{detail} ({duration_s:.0f}s)"
        self.store.save()

    def _known(self, call: str) -> str:
        call = normalize_call(call)
        if call not in {g.call for g in self.gateways()}:
            raise KeyError(f"unknown gateway {call}")
        return call

    # ---------------------------------------------------------------- ranking

    def day_or_night(self) -> Literal["day", "night"] | None:
        """'day' / 'night' at your grid (sun above/below -6 deg), None without a grid."""
        if not self.cfg.grid:
            return None
        lat, lon = grid_to_latlon(self.cfg.grid)
        return "day" if sun_elevation(lat, lon, self.now()) > -6 else "night"

    def rank(self, profile: Profile | None = None,
             profiles: list[Profile] | None = None) -> list[Ranked]:
        """Every known gateway, best first (unusable/blocked ones last).

        ``profile`` judges usability against one Winlink profile; ``profiles`` against any
        of a radio's Winlink profiles (a gateway is usable if a profile of its mode allows
        its frequency) - that is what the gateway panel shows.
        """
        period = self.day_or_night()
        out = []
        for gw in self.gateways():
            st = self.store.stats.get(gw.call, GatewayStats())
            fav = self.is_favorite(gw.call)
            blocked = gw.call in self.store.blocked
            reasons: list[str] = []
            score = 0.0
            if fav:
                score += 100
                reasons.append("favorite +100")
            rate_pts = 60 * st.rate()
            score += rate_pts
            reasons.append(f"history {st.successes}/{st.attempts} ok +{rate_pts:.0f}")
            if len(st.recent) >= 2 and not any(st.recent[-2:]):
                score -= 30
                reasons.append("last 2 attempts failed -30")
            dist = None
            if self.cfg.grid and gw.grid:
                dist = distance_km(self.cfg.grid, gw.grid)
                pen = min(dist / 10, 50)
                score -= pen
                reasons.append(f"{dist:.0f} km -{pen:.0f}")
            band = band_of(gw.freq)
            if (period and band and gw.freq < HF_LIMIT_HZ
                    and band in self.cfg.band_preference.get(period, [])):
                score += 20
                reasons.append(f"{band} preferred at {period} +20")
            usable = True
            if profile is not None:
                if profile.winlink_mode != gw.mode:
                    usable = False
                    reasons.append(f"mode {gw.mode} != profile {profile.winlink_mode}")
                elif not profile.freq_allowed(gw.freq):
                    usable = False
                    reasons.append("frequency outside profile freq_ranges")
            elif profiles is not None:
                same = [p for p in profiles if p.winlink_mode == gw.mode]
                if not same:
                    usable = False
                    reasons.append(f"no {gw.mode} profile on this radio")
                elif not any(p.freq_allowed(gw.freq) for p in same):
                    usable = False
                    reasons.append(f"frequency outside the {gw.mode} profile's freq_ranges")
            if blocked:
                reasons.append("blocked")
            out.append(Ranked(gw, score, reasons, fav, blocked, usable, dist, st))
        out.sort(key=lambda r: (r.blocked or not r.usable, -r.score,
                                r.distance_km if r.distance_km is not None else 1e9))
        return out

    def plan(self, profile: Profile, gateway: str | None = None) -> list[PlanStep]:
        """Ordered attempts for a Winlink check (empty if nothing usable).

        With ``gateway`` only that gateway is tried (it must be usable and not blocked).
        """
        ranked = [r for r in self.rank(profile) if r.usable and not r.blocked]
        if gateway is not None:
            call = normalize_call(gateway)
            ranked = [r for r in ranked if r.gateway.call == call]
        timeout = self.cfg.attempt_minutes * 60
        return [
            PlanStep(label=r.gateway.call, freq=r.gateway.freq, key=r.gateway.call,
                     run=(profile.run or "").replace("{call}", r.gateway.call),
                     timeout_s=timeout)
            # remote session (no run job): one gateway, held until release/expiry
            for r in ranked[: self.cfg.max_attempts if profile.run else 1]
        ]


__all__ = ["Gateway", "GatewayStore", "Ranked", "WinlinkPlanner", "band_of", "distance_km",
           "grid_to_latlon", "normalize_grid", "sun_elevation"]
