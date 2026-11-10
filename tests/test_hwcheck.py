"""Failure-point diagnosis: Digi-Rig missing vs rigctld down vs radio not answering."""

from __future__ import annotations

from pathlib import Path

import pytest

from rigbroker.config import InterfaceConfig
from rigbroker.hwcheck import (
    DIGIRIG_MISSING,
    RADIO_SILENT,
    RIG_ERROR,
    RIGCTLD_DOWN,
    InterfaceProbe,
    diagnose,
)
from rigbroker.rig_client import RigConnectionError, RigError, RigTimeout

SERIAL = "9846aeb62cc6ef1188be69527a5e3baa"
CARDS = """ 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 2 [Device         ]: USB-Audio - USB Audio Device
                      C-Media Electronics Inc. USB Audio Device at usb-xhci-hcd.1-2.2
"""


def make_probe(tmp_path: Path, *, serial: bool = True, audio: bool = True) -> InterfaceProbe:
    usb = tmp_path / "usb"
    (usb / "usb1").mkdir(parents=True)  # a root hub with no serial file
    if serial:
        (usb / "3-2.1").mkdir()
        (usb / "3-2.1" / "serial").write_text(SERIAL + "\n")
    (usb / "3-2.2").mkdir()
    (usb / "3-2.2" / "serial").write_text("other\n")
    cards = tmp_path / "cards"
    cards.write_text(CARDS if audio else CARDS.splitlines()[0] + "\n")
    cfg = InterfaceConfig(usb_serial=SERIAL, audio_card="Device")
    return InterfaceProbe(cfg, usb_root=str(usb), asound_cards=str(cards))


def test_probe_presence(tmp_path: Path) -> None:
    assert make_probe(tmp_path).snapshot() == {"serial": True, "audio": True}


def test_probe_missing(tmp_path: Path) -> None:
    p = make_probe(tmp_path, serial=False, audio=False)
    assert p.snapshot() == {"serial": False, "audio": False}


def test_probe_unconfigured_or_unreadable(tmp_path: Path) -> None:
    assert InterfaceProbe(InterfaceConfig()).snapshot() == {"serial": None, "audio": None}
    p = InterfaceProbe(InterfaceConfig(usb_serial="x", audio_card="y"),
                       usb_root=str(tmp_path / "nope"), asound_cards=str(tmp_path / "nope"))
    assert p.snapshot() == {"serial": None, "audio": None}  # can't tell -> don't blame it


@pytest.mark.parametrize(("exc", "code"), [
    (RigConnectionError("connect refused"), RIGCTLD_DOWN),
    (RigTimeout("no reply"), RADIO_SILENT),
    (RigError("get_freq failed: RPRT -5", -5), RADIO_SILENT),
    (RigError("get_freq failed: RPRT -6", -6), RADIO_SILENT),
    (RigError("set_powerstat failed: RPRT -11", -11), RIG_ERROR),
])
def test_diagnose_with_interface_present(tmp_path: Path, exc: RigError, code: str) -> None:
    d = diagnose(exc, probe=make_probe(tmp_path), rigctld="127.0.0.1:4542", radio="ID-4100A")
    assert d.code == code
    if code != RIG_ERROR:
        assert "Digi-Rig present on USB" in d.message


@pytest.mark.parametrize("exc", [RigConnectionError("refused"), RigTimeout("no reply")])
def test_missing_digirig_wins(tmp_path: Path, exc: RigError) -> None:
    d = diagnose(exc, probe=make_probe(tmp_path, serial=False), rigctld="h:1", radio="r")
    assert d.code == DIGIRIG_MISSING and "not detected on USB" in d.message


def test_diagnose_without_probe() -> None:
    assert diagnose(RigConnectionError("x"), probe=None, rigctld="h:1", radio="r").code \
        == RIGCTLD_DOWN
    assert diagnose(RigTimeout("x"), probe=None, rigctld="h:1", radio="r").code == RADIO_SILENT
