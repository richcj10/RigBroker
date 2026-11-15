"""Airband channel list: named channels from the config plus ones added in the web UI."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import AirbandChannel, AirbandConfig, Profile

log = logging.getLogger("rigbroker.airband")


class AirbandError(Exception):
    """A refused airband request (user-readable)."""


class AirbandChannels:
    def __init__(self, cfg: AirbandConfig, profile: Profile) -> None:
        self.cfg = cfg
        self.profile = profile
        self.path = Path(os.path.expanduser(cfg.state_file)) if cfg.state_file else None
        self.user: dict[int, AirbandChannel] = {}
        self.scan: list[int] = []  # scan list (Hz), in order
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.scan = [int(f) for f in raw.get("scan", [])]
                for c in raw.get("channels", []):
                    ch = AirbandChannel(**c)
                    self.user[ch.freq] = ch
            except (OSError, ValueError, TypeError) as exc:
                bad = self.path.with_suffix(f".corrupt-{int(time.time())}")
                log.error("airband list %s unreadable (%s); moved to %s", self.path, exc, bad)
                self.path.replace(bad)

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".airband-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"channels": [c.model_dump() for c in self.user.values()],
                       "scan": self.scan}, f, indent=1)
        os.replace(tmp, self.path)

    def channels(self) -> list[dict[str, Any]]:
        out = {c.freq: {**c.model_dump(), "source": "config"} for c in self.cfg.channels}
        out.update({f: {**c.model_dump(), "source": "user"} for f, c in self.user.items()})
        return sorted(out.values(), key=lambda c: (c["group"], c["freq"]))

    def add(self, ch: AirbandChannel) -> AirbandChannel:
        if not self.profile.freq_allowed(ch.freq):
            raise AirbandError(f"{ch.freq / 1e6:.3f} MHz is outside the airband range")
        self.user[ch.freq] = ch
        self._save()
        return ch

    def remove(self, freq: int) -> None:
        if freq not in self.user:
            raise KeyError("only channels added in the web UI can be removed")
        del self.user[freq]
        self._save()

    def set_scan(self, freqs: list[int]) -> list[int]:
        out: list[int] = []
        for f in freqs:
            if not self.profile.freq_allowed(f):
                raise AirbandError(f"{f / 1e6:.3f} MHz is outside the airband range")
            if f not in out:
                out.append(f)
        if len(out) > 100:
            raise AirbandError("at most 100 scan channels")
        self.scan = out
        self._save()
        return out

    def name_of(self, freq: int) -> str:
        for c in self.channels():
            if c["freq"] == freq:
                return f"{c['group']} {c['name']}".strip()
        return ""

    def info(self) -> dict[str, Any]:
        lo, hi = self.profile.freq_ranges[0]
        return {"radio": self.cfg.radio, "profile": self.cfg.profile, "step_hz": self.cfg.step_hz,
                "range": [lo, hi], "channels": self.channels(), "scan": self.scan}


__all__ = ["AirbandChannels", "AirbandError"]
