from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rigbroker.config import DirewolfConfig, DirewolfSettings
from rigbroker.direwolf import DirewolfManager, SettingsError, aprs_passcode, render

CALL = "KY4WL-5"


def mgr(tmp_path: Path, settings_file: bool = True) -> DirewolfManager:
    cfg = DirewolfConfig(conf_dir=str(tmp_path / "dw"),
                         settings_file=str(tmp_path / "dw.json") if settings_file else None)
    return DirewolfManager(cfg, CALL)


def test_passcode() -> None:
    assert aprs_passcode("N0CALL") == 13023  # well-known reference value
    assert aprs_passcode("n0call-10") == 13023  # SSID and case ignored


def test_render_aprs_full() -> None:
    s = DirewolfSettings(beacon=True, lat=37.12345, lon=-85.5, comment="RigBroker",
                         digipeater="fill-in", igate=True, igate_tx=True)
    text = render(s, CALL, "aprs")
    assert "MYCALL KY4WL-5" in text and "PTT RIG 2 localhost:4542" in text
    assert "KISSPORT 8001" in text and "AGWPORT 0" in text
    assert ('PBEACON delay=1 every=30 symbol="/-" lat=37^07.41N long=085^30.00W '
            'via=WIDE1-1,WIDE2-1 comment="RigBroker"') in text
    assert "DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$" in text
    assert f"IGLOGIN KY4WL-5 {aprs_passcode(CALL)}" in text and "IGTXVIA 0" in text
    masked = render(s, CALL, "aprs", mask_secrets=True)
    assert "IGLOGIN KY4WL-5 *****" in masked and str(aprs_passcode(CALL)) not in masked


def test_render_quiet_and_packet_instances() -> None:
    s = DirewolfSettings(beacon=True, lat=1, lon=2, digipeater="wide", igate=True, igate_tx=True)
    rx = render(s, CALL, "aprs-rx")
    assert "PTT" not in rx.replace("# no PTT", "")  # physically can't key
    assert "PBEACON" not in rx and "DIGIPEAT" not in rx and "IGTXVIA" not in rx
    assert "IGLOGIN" in rx  # RX-only iGate still fine
    pkt = render(s, CALL, "pkt")
    # KISS on its own port for Winlink Express over the LAN
    assert "AGWPORT 8000" in pkt and "KISSPORT 8010" in pkt and "PTT RIG 2" in pkt
    assert "PBEACON" not in pkt and "DIGIPEAT" not in pkt
    assert "IGSERVER" not in pkt and "IGLOGIN" not in pkt


def test_southern_eastern_and_rounding() -> None:
    s = DirewolfSettings(beacon=True, lat=-33.999999, lon=151.2)
    t = render(s, CALL, "aprs")
    assert "lat=34^00.00S long=151^12.00E" in t


@pytest.mark.parametrize("change", [
    {"comment": 'x"\nIGLOGIN EVIL 1'},          # quote + newline injection
    {"comment": "a" * 44},
    {"adevice": "plughw:0\nPTT GPIO 4"},
    {"igate_server": "evil.example\nKISSPORT 1"},
    {"symbol": '/"'},
    {"symbol": "abc"},
    {"beacon_path": ["WIDE3-3"]},
    {"beacon_path": ["WIDE1-1", "WIDE2-1", "WIDE2-2"]},
    {"beacon": True},                             # no position
    {"beacon_every_min": 5},                      # too frequent
    {"kiss_port": 8000},                          # clashes with agw
    {"pkt_kiss_port": 8001},                      # clashes with the APRS KISS port
    {"passcode": 99999},
    {"unknown_field": 1},
])
def test_rejects_bad_settings(tmp_path: Path, change: dict[str, object]) -> None:
    m = mgr(tmp_path)
    before = m.settings
    with pytest.raises(SettingsError):
        m.update(change)
    assert m.settings == before  # nothing half-applied


def test_update_persist_and_write(tmp_path: Path) -> None:
    m = mgr(tmp_path)
    m.update({"beacon": True, "lat": 37.0, "lon": -86.0, "passcode": 12345})
    assert m.public_settings()["passcode"] == "set"
    again = mgr(tmp_path)  # reloaded from the settings file
    assert again.settings.beacon and again.settings.passcode == 12345
    paths = again.write()
    assert sorted(p.name for p in paths) == ["aprs-rx.conf", "aprs.conf", "pkt.conf"]
    if os.name == "posix":
        for p in paths:
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "dw.json").stat().st_mode) == 0o600
    assert "PBEACON" in (tmp_path / "dw" / "aprs.conf").read_text()


def test_corrupt_settings_file_falls_back(tmp_path: Path) -> None:
    (tmp_path / "dw.json").write_text('{"comment": "bad\\"quote"}')
    m = mgr(tmp_path)
    assert m.settings == DirewolfSettings()
    assert list(tmp_path.glob("dw.invalid-*"))
