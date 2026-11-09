"""Configuration models and loader.

The whole config is validated with pydantic at startup; any problem raises
:class:`ConfigError` with one line per problem (``radios.vu.profiles.aprs.power: ...``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_NAME_RE = re.compile(r"^[a-z0-9_]+$")
# Gateway callsigns end up in a command line: keep them strictly alphanumeric (+ SSID).
CALL_RE = re.compile(r"^[A-Z0-9]{3,8}(-[0-9]{1,2})?$")
GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-X]{2})?$")

WinlinkMode = Literal["packet", "vara_fm", "vara_hf", "ardop"]


def normalize_call(v: str) -> str:
    """Upper-case and validate a callsign(-SSID). Raises ValueError."""
    call = v.strip().upper()
    if not CALL_RE.match(call):
        raise ValueError(f"invalid callsign {v!r} (letters/digits, optional -SSID)")
    return call


def normalize_grid(v: str | None) -> str | None:
    """Maidenhead locator, 4 or 6 characters (e.g. EM85 or EM85ab). Raises ValueError."""
    if v is None or v == "":
        return None
    g = v.strip()
    g = g[:4].upper() + g[4:].upper()
    if not GRID_RE.match(g):
        raise ValueError(f"invalid grid square {v!r} (e.g. EM85 or EM85ab)")
    return g[:4] + g[4:].lower()


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Profile(_Strict):
    """One way of using a radio (frequency, mode, power, consumer service)."""

    freq: int | None = Field(default=None, gt=0, description="Frequency in Hz")
    mode: str | None = None
    power: float | None = Field(default=None, ge=0.0, le=1.0, description="RFPOWER level 0..1")
    squelch: float | None = Field(default=None, ge=0.0, le=1.0, description="SQL level 0..1")
    service: str | None = None
    rx_service: str | None = Field(
        default=None,
        description="receive-only consumer used instead of `service` when TX isn't allowed "
                    "(e.g. direwolf@aprs-rx, whose config has no PTT line)",
    )
    run: str | None = None
    max_minutes: float | None = Field(default=None, gt=0)
    preemptible: bool = False
    transmits: bool = False
    rig_power: Literal["on", "off"] = "on"
    winlink_mode: WinlinkMode | None = Field(
        default=None, description="Winlink gateway profile: run must contain {call}"
    )
    freq_ranges: list[tuple[int, int]] = Field(
        default_factory=list, description="[low, high] Hz ranges a gateway frequency may use"
    )

    @field_validator("mode")
    @classmethod
    def _upper_mode(cls, v: str | None) -> str | None:
        return v.upper() if v else v

    @field_validator("freq_ranges")
    @classmethod
    def _ranges(cls, v: list[tuple[int, int]]) -> list[tuple[int, int]]:
        for lo, hi in v:
            if not 0 < lo < hi:
                raise ValueError(f"bad range [{lo}, {hi}]: need 0 < low < high")
        return v

    def freq_allowed(self, hz: int) -> bool:
        """True if ``hz`` lies inside one of this profile's freq_ranges."""
        return any(lo <= hz <= hi for lo, hi in self.freq_ranges)

    @model_validator(mode="after")
    def _consistent(self) -> Profile:
        if self.rig_power == "off":
            if self.transmits:
                raise ValueError("a profile with rig_power: off cannot transmit")
            if self.run:
                raise ValueError("a profile with rig_power: off cannot have a run command")
            if any(v is not None for v in (self.freq, self.mode, self.power, self.squelch)):
                raise ValueError(
                    "a profile with rig_power: off must not set freq/mode/power/squelch"
                )
        elif self.freq is None or self.mode is None:
            raise ValueError("freq and mode are required (unless rig_power: off)")
        if self.transmits and self.power is None:
            raise ValueError("transmitting profiles must set power (it is read-back verified)")
        has_call = bool(self.run and "{call}" in self.run)
        if self.winlink_mode:
            # no run = "remote session": tune to the gateway and hold the modem for a
            # Winlink client on another computer (e.g. Winlink Express over the LAN)
            if self.run and not has_call:
                raise ValueError("winlink_mode profiles need run containing {call}, "
                                 "e.g. 'pat connect ax25:///{call}' (or no run at all for "
                                 "a remote Winlink client)")
            if not self.freq_ranges:
                raise ValueError("winlink_mode profiles need freq_ranges (allowed gateway freqs)")
            if not self.transmits:
                raise ValueError("winlink_mode profiles must be transmits: true")
        elif has_call:
            raise ValueError("run uses {call} but the profile has no winlink_mode")
        if self.rx_service is not None and (not self.transmits or self.rx_service == self.service):
            raise ValueError("rx_service is only for transmitting profiles and must differ "
                             "from service")
        if self.freq is not None and self.freq_ranges and not self.freq_allowed(self.freq):
            raise ValueError(f"freq {self.freq} is outside this profile's freq_ranges")
        return self


class Hooks(_Strict):
    """Optional shell commands run during fault handling (via the JobRunner)."""

    usb_power_cycle: str | None = None
    rig_power_cut: str | None = None


class RadioConfig(_Strict):
    """One radio: its rigctld endpoint, profiles and default profile."""

    name: str
    rigctld: str = Field(description="host:port of this radio's rigctld")
    rigctld_service: str | None = Field(
        default=None, description="systemd unit restarted during fault recovery"
    )
    default_profile: str
    winlink_profile: str | None = Field(
        default=None, description="profile used by the 'Winlink check' button/command"
    )
    profiles: dict[str, Profile] = Field(default_factory=dict)
    hooks: Hooks = Field(default_factory=Hooks)

    @field_validator("rigctld")
    @classmethod
    def _host_port(cls, v: str) -> str:
        host, sep, port = v.rpartition(":")
        if not sep or not host:
            raise ValueError("must be host:port, e.g. 127.0.0.1:4542")
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"invalid port {port!r}")
        return v

    @field_validator("profiles")
    @classmethod
    def _profile_names(cls, v: dict[str, Profile]) -> dict[str, Profile]:
        for name in v:
            if not _NAME_RE.match(name):
                raise ValueError(f"profile name {name!r} must match [a-z0-9_]+")
        return v

    @model_validator(mode="after")
    def _default_exists(self) -> RadioConfig:
        if self.profiles and self.default_profile not in self.profiles:
            raise ValueError(
                f"default_profile {self.default_profile!r} is not one of the profiles: "
                f"{', '.join(sorted(self.profiles))}"
            )
        if self.winlink_profile is not None and self.winlink_profile not in self.profiles:
            raise ValueError(f"winlink_profile {self.winlink_profile!r} is not one of the profiles")
        return self

    @property
    def enabled(self) -> bool:
        """A radio with no profiles is configured but not managed yet."""
        return bool(self.profiles)

    @property
    def host(self) -> str:
        return self.rigctld.rpartition(":")[0]

    @property
    def port(self) -> int:
        return int(self.rigctld.rpartition(":")[2])

    def tx_services(self) -> set[str]:
        """Services of profiles that transmit (stopped on any fault)."""
        return {p.service for p in self.profiles.values() if p.transmits and p.service}

    def all_services(self) -> set[str]:
        """Every consumer service this radio may run (including RX-only fallbacks)."""
        return ({p.service for p in self.profiles.values() if p.service}
                | {p.rx_service for p in self.profiles.values() if p.rx_service})


class SafetyConfig(_Strict):
    """Timings and limits for the safety rules. All durations in seconds."""

    tx_holdoff_s: float = Field(default=2.0, ge=0)
    clear_channel_timeout_s: float = Field(default=30.0, gt=0)
    clear_channel_hold_s: float = Field(default=1.0, ge=0)
    stuck_tx_s: float = Field(default=90.0, gt=0)
    poll_interval_s: float = Field(default=1.0, gt=0)
    poll_backoff_max_s: float = Field(default=10.0, gt=0)
    offline_after_failures: int = Field(default=3, ge=1)
    rig_timeout_s: float = Field(default=2.0, gt=0)
    power_on_timeout_s: float = Field(default=10.0, gt=0)
    freq_tolerance_hz: int = Field(default=0, ge=0)
    level_tolerance: float = Field(default=0.02, ge=0)
    default_lease_minutes: float = Field(default=30.0, gt=0)
    lease_grace_s: float = Field(default=5.0, ge=0)
    recovery_attempts: int = Field(default=3, ge=1)
    recovery_backoff_s: float = Field(default=5.0, ge=0)
    max_auto_recoveries: int = Field(default=3, ge=0)
    recovery_restart_rigctld: bool = Field(
        default=False,
        description="restart rigctld during fault recovery; off by default because every "
                    "rigctld start pulses a Digirig's RTS/PTT for ~55 ms",
    )
    fault_window_s: float = Field(default=600.0, gt=0)
    max_queue: int = Field(default=10, ge=0)


class MqttConfig(_Strict):
    """MQTT broker connection (used from milestone 3)."""

    host: str = "127.0.0.1"
    port: int = Field(default=1883, gt=0, lt=65536)
    base_topic: str = "rigbroker"
    discovery_prefix: str = "homeassistant"
    username: str | None = None
    password: str | None = None


class GatewayConfig(_Strict):
    """A Winlink RMS gateway you know about (curated; usually your known-good spots)."""

    call: str
    freq: int = Field(gt=0, description="dial frequency in Hz")
    mode: WinlinkMode
    grid: str | None = None
    favorite: bool = False
    note: str = Field(default="", max_length=200)

    @field_validator("call")
    @classmethod
    def _call(cls, v: str) -> str:
        return normalize_call(v)

    @field_validator("grid")
    @classmethod
    def _grid(cls, v: str | None) -> str | None:
        return normalize_grid(v)


DayNight = Literal["day", "night"]


def _default_bands() -> dict[DayNight, list[str]]:
    return {"day": ["20m", "30m", "40m"], "night": ["80m", "40m", "30m"]}


class WinlinkConfig(_Strict):
    """Gateway planner settings."""

    grid: str | None = Field(default=None, description="your Maidenhead locator")
    max_attempts: int = Field(default=3, ge=1, le=10)
    attempt_minutes: float = Field(default=4.0, gt=0, description="per-gateway time limit")
    state_file: str | None = Field(
        default="~/.local/state/rigbroker/winlink.json",
        description="favorites/blocklist/history; null = memory only",
    )
    band_preference: dict[DayNight, list[str]] = Field(
        default_factory=_default_bands,
        description="HF bands favoured by day / by night at your location",
    )
    gateways: list[GatewayConfig] = Field(default_factory=list)

    @field_validator("grid")
    @classmethod
    def _grid(cls, v: str | None) -> str | None:
        return normalize_grid(v)

    @field_validator("gateways")
    @classmethod
    def _unique(cls, v: list[GatewayConfig]) -> list[GatewayConfig]:
        seen: set[str] = set()
        for g in v:
            if g.call in seen:
                raise ValueError(f"gateway {g.call} listed twice")
            seen.add(g.call)
        return v


class ApiConfig(_Strict):
    """REST/WebSocket/web UI listener."""

    host: str = "0.0.0.0"
    port: int = Field(default=8080, gt=0, lt=65536)
    status_interval_s: float = Field(default=1.0, gt=0)


_PATH_RE = re.compile(r"^WIDE[12]-[12]$")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9:,=_.\-]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
# Characters allowed in free text that ends up in a config file / APRS packet:
# printable ASCII, minus quote/backslash (config syntax) and |~{ (APRS reserved).
_TEXT_RE = re.compile(r'^[ !#-\[\]-z}]*$')


def _check_path(v: list[str]) -> list[str]:
    out = [p.strip().upper() for p in v]
    if len(out) > 2 or any(not _PATH_RE.match(p) for p in out):
        raise ValueError("path: at most 2 hops from WIDE1-1, WIDE2-1, WIDE2-2")
    return out


class DirewolfSettings(BaseModel):
    """Direwolf settings the broker renders into config files (editable in the UI)."""

    model_config = ConfigDict(extra="forbid")

    adevice: str = Field(default="plughw:CARD=Device,DEV=0",
                         description="ALSA device of the Digirig (see `aplay -l`)")
    arate: Literal[44100, 48000] = 48000
    rigctld_port: int = Field(default=4542, ge=1024, le=65535,
                              description="PTT through rigctld (Hamlib NET rig)")
    txdelay: int = Field(default=30, ge=5, le=100, description="units of 10 ms")
    txtail: int = Field(default=10, ge=1, le=50, description="units of 10 ms")
    kiss_port: int = Field(default=8001, ge=1024, le=65535,
                           description="APRS instance KISS (the broker's own APRS link)")
    agw_port: int = Field(default=8000, ge=1024, le=65535)
    pkt_kiss_port: int = Field(default=8010, ge=1024, le=65535,
                               description="packet (Winlink) instance KISS, for Winlink "
                                           "Express over the LAN")
    beacon: bool = False
    beacon_every_min: int = Field(default=30, ge=10, le=1440)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    symbol: str = Field(default="/-", description="APRS symbol table+code, e.g. /- house")
    comment: str = Field(default="", max_length=43)
    beacon_path: list[str] = Field(default_factory=lambda: ["WIDE1-1", "WIDE2-1"])
    digipeater: Literal["off", "fill-in", "wide"] = "off"
    igate: bool = False
    igate_server: str = "noam.aprs2.net"
    igate_tx: bool = Field(default=False, description="gate APRS-IS messages to RF")
    passcode: int | None = Field(default=None, ge=0, le=32767,
                                 description="APRS-IS passcode; computed from the call if unset")

    @field_validator("adevice")
    @classmethod
    def _dev(cls, v: str) -> str:
        if not _DEVICE_RE.match(v):
            raise ValueError("audio device: letters, digits and :,=_.- only")
        return v

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        if len(v) != 2 or v[0] not in "/\\0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" \
                or not 33 <= ord(v[1]) <= 126 or v[1] in "\"\\|~{":
            raise ValueError("symbol: 2 chars, table (/ or \\ or overlay) + code, e.g. /-")
        return v

    @field_validator("comment")
    @classmethod
    def _comment(cls, v: str) -> str:
        if not _TEXT_RE.match(v):
            raise ValueError('comment: printable ASCII without " \\ | ~ {')
        return v

    @field_validator("beacon_path")
    @classmethod
    def _bpath(cls, v: list[str]) -> list[str]:
        return _check_path(v)

    @field_validator("igate_server")
    @classmethod
    def _server(cls, v: str) -> str:
        if not _HOST_RE.match(v):
            raise ValueError("igate_server: hostname only")
        return v

    @model_validator(mode="after")
    def _beacon_needs_position(self) -> DirewolfSettings:
        if self.beacon and (self.lat is None or self.lon is None):
            raise ValueError("beacon needs lat and lon")
        if len({self.kiss_port, self.agw_port, self.pkt_kiss_port}) != 3:
            raise ValueError("kiss_port, agw_port and pkt_kiss_port must all differ")
        return self


class DirewolfConfig(_Strict):
    """Where generated Direwolf configs and UI-edited settings live."""

    conf_dir: str = "~/.local/state/rigbroker/direwolf"
    settings_file: str | None = Field(
        default="~/.local/state/rigbroker/direwolf-settings.json",
        description="settings edited in the UI (overlay on `defaults`); null = memory",
    )
    defaults: DirewolfSettings = Field(default_factory=DirewolfSettings)


class AprsConfig(_Strict):
    """APRS station: messaging, heard stations, map."""

    callsign: str = Field(description="this station, e.g. KY4WL-5 (messages to it are acked)")
    other_calls: list[str] = Field(
        default_factory=list, description="your other calls: shown, never acked from here")
    radio: str = "vu"
    tx_service: str = Field(default="direwolf@aprs",
                            description="consumer that must be running to transmit")
    rx_services: list[str] = Field(
        default_factory=lambda: ["direwolf@aprs", "direwolf@aprs-rx"])
    kiss: str = "127.0.0.1:8001"
    tocall: str = Field(default="APZRBK", description="APZ = experimental software")
    msg_path: list[str] = Field(default_factory=lambda: ["WIDE1-1", "WIDE2-1"])
    msg_retries: int = Field(default=5, ge=1, le=10)
    msg_retry_s: float = Field(default=30.0, gt=0)
    station_timeout_min: float = Field(default=120.0, gt=0)
    max_packets: int = Field(default=500, ge=10, le=10000)

    @field_validator("callsign")
    @classmethod
    def _call(cls, v: str) -> str:
        return normalize_call(v)

    @field_validator("other_calls")
    @classmethod
    def _others(cls, v: list[str]) -> list[str]:
        return [normalize_call(c) for c in v]

    @field_validator("msg_path")
    @classmethod
    def _mpath(cls, v: list[str]) -> list[str]:
        return _check_path(v)

    @field_validator("kiss")
    @classmethod
    def _kiss(cls, v: str) -> str:
        host, sep, port = v.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError("kiss must be host:port")
        return v

    @property
    def kiss_host(self) -> str:
        return self.kiss.rpartition(":")[0]

    @property
    def kiss_port(self) -> int:
        return int(self.kiss.rpartition(":")[2])


class SecurityConfig(_Strict):
    """Web/API access control (one shared station password + API tokens)."""

    auth_file: str | None = Field(
        default="~/.config/rigbroker/auth.json",
        description="password hash, session key and token hashes (mode 0600); null = memory",
    )
    public_view: bool = Field(default=True, description="status visible without login")
    session_hours: float = Field(default=12.0, gt=0, le=720)
    max_failures: int = Field(default=5, ge=1, description="failed logins before lockout")
    lockout_minutes: float = Field(default=5.0, gt=0)


class Config(_Strict):
    """Root configuration."""

    radios: dict[str, RadioConfig]
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    winlink: WinlinkConfig = Field(default_factory=WinlinkConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    aprs: AprsConfig | None = None
    direwolf: DirewolfConfig = Field(default_factory=DirewolfConfig)

    @model_validator(mode="after")
    def _aprs_radio(self) -> Config:
        if self.aprs is not None:
            r = self.radios.get(self.aprs.radio)
            if r is None or not r.enabled:
                raise ValueError(f"aprs.radio {self.aprs.radio!r} is not an enabled radio")
            if self.aprs.tx_service not in r.all_services():
                raise ValueError(f"aprs.tx_service {self.aprs.tx_service!r} is not a service "
                                 f"of radio {self.aprs.radio!r}")
        return self

    @field_validator("radios")
    @classmethod
    def _radios(cls, v: dict[str, RadioConfig]) -> dict[str, RadioConfig]:
        for rid in v:
            if not _NAME_RE.match(rid):
                raise ValueError(f"radio id {rid!r} must match [a-z0-9_]+")
        if not any(r.enabled for r in v.values()):
            raise ValueError("at least one radio needs profiles")
        return v

    def enabled_radios(self) -> dict[str, RadioConfig]:
        return {k: r for k, r in self.radios.items() if r.enabled}


def _format(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def _check_yaml_booleans(data: dict[str, Any], source: str) -> None:
    """YAML 1.1 reads unquoted off/on/yes/no as booleans; explain instead of a cryptic error."""
    radios = data.get("radios")
    if not isinstance(radios, dict):
        return
    for rid, radio in radios.items():
        if not isinstance(radio, dict):
            continue
        profiles = radio.get("profiles")
        names = list(profiles) if isinstance(profiles, dict) else []
        for name in [*names, radio.get("default_profile")]:
            if isinstance(name, bool):
                raise ConfigError(
                    f"{source}: radios.{rid}: a profile name was read as the boolean {name}; "
                    "YAML treats unquoted off/on/yes/no as true/false - quote it, e.g. \"off\""
                )


def parse_config(data: Any, source: str = "<config>") -> Config:
    """Validate an already-parsed mapping. Raises ConfigError."""
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping")
    _check_yaml_booleans(data, source)
    mqtt = data.get("mqtt")
    if isinstance(mqtt, dict):
        # Secrets come from the environment, never the repo.
        for key in ("username", "password"):
            env = os.environ.get(f"RIGBROKER_MQTT_{key.upper()}")
            if env:
                mqtt[key] = env
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{source}: invalid configuration\n{_format(exc)}") from None


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML config file. Raises ConfigError with a readable message."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {p}: {exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: YAML syntax error: {exc}") from None
    return parse_config(data, str(p))
