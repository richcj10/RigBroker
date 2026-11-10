"""Where is the radio link broken? Tells apart the failure points between the broker and
the radio so the logs and the UI say *which* one failed:

    Digi-Rig USB (serial chip)  ->  rigctld  ->  CI-V  ->  radio
    Digi-Rig USB (sound card)   ->  Direwolf / VARA

The checks only READ sysfs and /proc/asound. They never open the serial port: a first
open of the Digi-Rig tty raises RTS, which keys the radio.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import InterfaceConfig
from .rig_client import RigConnectionError, RigError, RigTimeout

# failure codes (also used as event kinds)
DIGIRIG_MISSING = "digirig_missing"
RIGCTLD_DOWN = "rigctld_down"
RADIO_SILENT = "radio_not_answering"
RIG_ERROR = "rig_error"
AUDIO_MISSING = "digirig_audio_missing"

FAILURE_LABELS = {
    DIGIRIG_MISSING: "Digi-Rig not detected on USB",
    RIGCTLD_DOWN: "rigctld not running",
    RADIO_SILENT: "radio not answering CI-V",
    RIG_ERROR: "rig command error",
    AUDIO_MISSING: "Digi-Rig sound card missing",
}

# rigctld's codes for "the radio didn't answer / the line is bad": Hamlib RIG_ETIMEOUT (-5),
# RIG_EIO (-6), RIG_EPROTO (-8, garbled CI-V), RIG_BUSERROR (-13), RIG_BUSBUSY (-14).
# Anything else (e.g. -11 "not available") is a real command error, not a dead link.
_NO_ANSWER_CODES = {-5, -6, -8, -13, -14}


@dataclass(frozen=True)
class Diagnosis:
    code: str
    message: str


class InterfaceProbe:
    """Checks that the radio interface (e.g. a Digi-Rig) is present on USB."""

    def __init__(self, cfg: InterfaceConfig, *, usb_root: str = "/sys/bus/usb/devices",
                 asound_cards: str = "/proc/asound/cards") -> None:
        self.cfg = cfg
        self.usb_root = Path(usb_root)
        self.asound_cards = Path(asound_cards)

    def serial_present(self) -> bool | None:
        """True/False if the USB serial chip is plugged in; None if not configured."""
        want = self.cfg.usb_serial
        if not want:
            return None
        try:
            entries = list(os.scandir(self.usb_root))
        except OSError:
            return None  # can't tell (no sysfs): don't blame the interface
        for e in entries:
            try:
                if (Path(e.path) / "serial").read_text().strip() == want:
                    return True
            except OSError:
                continue
        return False

    def audio_present(self) -> bool | None:
        """True/False if the interface's ALSA sound card exists; None if not configured."""
        card = self.cfg.audio_card
        if not card:
            return None
        try:
            text = self.asound_cards.read_text()
        except OSError:
            return None
        return any(line.split("[", 1)[1].split("]", 1)[0].strip() == card
                   for line in text.splitlines() if "[" in line and "]" in line)

    def snapshot(self) -> dict[str, bool | None]:
        return {"serial": self.serial_present(), "audio": self.audio_present()}


def diagnose(exc: RigError, *, probe: InterfaceProbe | None, rigctld: str,
             radio: str) -> Diagnosis:
    """Name the failure point for a failed poll."""
    iface = probe.cfg.name if probe else "radio interface"
    if probe is not None and probe.serial_present() is False:
        return Diagnosis(DIGIRIG_MISSING,
                         f"{iface} not detected on USB (serial {probe.cfg.usb_serial} missing): "
                         "unplugged, bad USB cable or hub")
    present = f"{iface} present on USB; " if probe and probe.cfg.usb_serial else ""
    if isinstance(exc, RigConnectionError):
        return Diagnosis(RIGCTLD_DOWN,
                         f"{present}rigctld not reachable at {rigctld} "
                         f"(service stopped or restarting): {exc}")
    if isinstance(exc, RigTimeout) or exc.code in _NO_ANSWER_CODES:
        return Diagnosis(RADIO_SILENT,
                         f"{present}rigctld running, but the {radio} does not answer CI-V "
                         f"(radio off, CI-V cable, or wrong CI-V address/baud): {exc}")
    return Diagnosis(RIG_ERROR, f"rig command failed: {exc}")
