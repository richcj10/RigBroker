"""TX power levels chosen in the web UI, per radio and profile (kept on the Pi, not in git).

Only the radio's named levels (``power_levels`` in the config, e.g. L/M/H) can be chosen,
never above ``max_power_level``. The file holds ``{radio: {profile: level}}``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("rigbroker.power")


class PowerStore:
    """Tiny JSON store; ``path=None`` keeps everything in memory (tests, mock mode)."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path).expanduser() if path else None
        self._data: dict[str, dict[str, str]] = {}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                self._data = {str(r): {str(p): str(lv) for p, lv in d.items()}
                              for r, d in raw.items() if isinstance(d, dict)}
            except (OSError, ValueError, AttributeError) as exc:
                log.warning("ignoring unreadable power file %s: %s", self.path, exc)

    def get(self, radio: str, profile: str) -> str | None:
        return self._data.get(radio, {}).get(profile)

    def set(self, radio: str, profile: str, level: str | None) -> None:
        d = self._data.setdefault(radio, {})
        if level is None:
            d.pop(profile, None)
        else:
            d[profile] = level
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".power.")
        with os.fdopen(fd, "w") as f:
            json.dump(self._data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
