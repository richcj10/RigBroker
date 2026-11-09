from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from rigbroker.config import ConfigError, load_config, parse_config
from tests.conftest import BASE

ROOT = Path(__file__).resolve().parent.parent


def _cfg() -> dict[str, Any]:
    return copy.deepcopy(BASE)


def _err(data: dict[str, Any]) -> str:
    with pytest.raises(ConfigError) as ei:
        parse_config(data)
    return str(ei.value)


def test_example_config_loads() -> None:
    cfg = load_config(ROOT / "config" / "profiles.yaml")
    assert list(cfg.enabled_radios()) == ["vu"]
    vu = cfg.radios["vu"]
    assert vu.port == 4542 and vu.host == "127.0.0.1"
    assert vu.profiles["aprs"].freq == 144_390_000
    assert vu.tx_services() == {"direwolf@aprs", "direwolf@pkt", "vara-fm"}
    assert cfg.safety.stuck_tx_s == 90
    assert not cfg.radios["hf"].enabled


def test_power_out_of_range() -> None:
    d = _cfg()
    d["radios"]["vu"]["profiles"]["aprs"]["power"] = 1.5
    assert "radios.vu.profiles.aprs.power" in _err(d)


def test_unknown_key_rejected() -> None:
    d = _cfg()
    d["radios"]["vu"]["profiles"]["aprs"]["frq"] = 1
    msg = _err(d)
    assert "aprs.frq" in msg and "Extra inputs" in msg


def test_default_profile_must_exist() -> None:
    d = _cfg()
    d["radios"]["vu"]["default_profile"] = "nope"
    assert "default_profile 'nope' is not one of the profiles" in _err(d)


def test_transmitting_profile_needs_power() -> None:
    d = _cfg()
    del d["radios"]["vu"]["profiles"]["vara_fm"]["power"]
    assert "must set power" in _err(d)


def test_freq_and_mode_required() -> None:
    d = _cfg()
    del d["radios"]["vu"]["profiles"]["monitor"]["freq"]
    assert "freq and mode are required" in _err(d)


def test_off_profile_cannot_transmit_or_tune() -> None:
    d = _cfg()
    d["radios"]["vu"]["profiles"]["off"] = {"rig_power": "off", "transmits": True}
    assert "cannot transmit" in _err(d)
    d["radios"]["vu"]["profiles"]["off"] = {"rig_power": "off", "freq": 1}
    assert "must not set freq" in _err(d)


@pytest.mark.parametrize("addr", ["4542", "localhost:", "host:0", "host:99999", "host:abc"])
def test_bad_rigctld_address(addr: str) -> None:
    d = _cfg()
    d["radios"]["vu"]["rigctld"] = addr
    assert "radios.vu.rigctld" in _err(d)


def test_negative_safety_value() -> None:
    d = _cfg()
    d["safety"]["stuck_tx_s"] = -1
    assert "safety.stuck_tx_s" in _err(d)


def test_needs_an_enabled_radio() -> None:
    d = _cfg()
    del d["radios"]["vu"]
    assert "at least one radio needs profiles" in _err(d)


def test_bad_names() -> None:
    d = _cfg()
    d["radios"]["vu"]["profiles"]["Bad-Name"] = {"rig_power": "off"}
    assert "must match" in _err(d)


def test_mode_is_uppercased() -> None:
    d = _cfg()
    d["radios"]["vu"]["profiles"]["monitor"]["mode"] = "fm"
    assert parse_config(d).radios["vu"].profiles["monitor"].mode == "FM"


def test_mqtt_secrets_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIGBROKER_MQTT_PASSWORD", "s3cret")
    d = _cfg()
    d["mqtt"] = {"host": "ha.local", "username": "rb"}
    cfg = parse_config(d)
    assert cfg.mqtt.password == "s3cret" and cfg.mqtt.username == "rb"


def test_winlink_profile_rules() -> None:
    d = _cfg()
    d["radios"]["vu"]["profiles"]["winlink_gw"]["run"] = "pat connect ax25:///W4X-10"
    assert "need run containing {call}" in _err(d)
    d = _cfg()
    d["radios"]["vu"]["profiles"]["winlink_gw"]["freq_ranges"] = []
    assert "need freq_ranges" in _err(d)
    d = _cfg()
    d["radios"]["vu"]["profiles"]["winlink_gw"]["freq"] = 440_000_000
    assert "outside this profile's freq_ranges" in _err(d)
    d = _cfg()
    d["radios"]["vu"]["profiles"]["monitor"]["run"] = "x {call}"
    assert "has no winlink_mode" in _err(d)


def test_winlink_gateway_rules() -> None:
    d = _cfg()
    d["winlink"]["gateways"].append(dict(d["winlink"]["gateways"][0]))
    assert "listed twice" in _err(d)
    d = _cfg()
    d["winlink"]["gateways"][0]["call"] = "W4X;reboot"
    assert "invalid callsign" in _err(d)
    d = _cfg()
    d["winlink"]["grid"] = "ZZ99"
    assert "invalid grid square" in _err(d)
    d = _cfg()
    d["winlink"]["gateways"][0]["mode"] = "pactor"
    assert "winlink.gateways.0.mode" in _err(d)


def test_unquoted_off_explained(tmp_path: Path) -> None:
    f = tmp_path / "c.yaml"
    f.write_text("radios:\n  vu:\n    name: x\n    rigctld: 127.0.0.1:4542\n"
                 "    default_profile: monitor\n    profiles:\n"
                 "      monitor: {freq: 1, mode: FM}\n      off: {rig_power: 'off'}\n")
    with pytest.raises(ConfigError, match=r'quote it, e\.g\. "off"'):
        load_config(f)


def test_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("radios: [unclosed\n")
    with pytest.raises(ConfigError, match="YAML syntax error"):
        load_config(bad)
    bad.write_text("- just a list\n")
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_config(bad)
