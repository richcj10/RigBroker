"""``rigbroker`` / ``python -m rigbroker`` entry point: broker + REST/WebSocket/web UI.

Modes:
  --mock            everything simulated (demo / UI work)
  --mock-services   real rigctld (e.g. Hamlib dummy), simulated systemd services and jobs
  (neither)         hardware: rigctld + systemd units + real commands.
                    Read-only unless --allow-tx: tunes the rig, never starts anything that
                    can transmit (APRS falls back to its receive-only rx_service).
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .api import create_app
from .broker import Broker
from .config import Config, ConfigError, load_config
from .logging_setup import setup_logging


def _args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rigbroker", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/profiles.yaml", help="profiles YAML")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true",
                      help="in-memory rigs/services/jobs; never touches hardware or systemd")
    mode.add_argument("--mock-services", action="store_true",
                      help="real rigctld, simulated services and jobs (Hamlib dummy testing)")
    p.add_argument("--allow-tx", action="store_true",
                   help="allow starting TX-capable consumers and run jobs")
    p.add_argument("--rigctld", action="append", default=[], metavar="RADIO=HOST:PORT",
                   help="override a radio's rigctld address (e.g. vu=127.0.0.1:4599)")
    p.add_argument("--host", help="override api.host")
    p.add_argument("--port", type=int, help="override api.port")
    p.add_argument("--log-json", action="store_true", help="JSON log lines")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--check-config", action="store_true", help="validate config and exit")
    return p.parse_args(argv)


def _override_rigctld(cfg: Config, specs: list[str]) -> Config:
    radios = dict(cfg.radios)
    for spec in specs:
        rid, sep, addr = spec.partition("=")
        if not sep or rid not in radios:
            raise ConfigError(f"--rigctld {spec!r}: expected RADIO=HOST:PORT with a known radio")
        host, _, port = addr.rpartition(":")
        if not host or not port.isdigit():
            raise ConfigError(f"--rigctld {spec!r}: bad HOST:PORT")
        radios[rid] = radios[rid].model_copy(update={"rigctld": addr})
    return cfg.model_copy(update={"radios": radios})


def main(argv: list[str] | None = None) -> int:
    a = _args(argv)
    level = getattr(logging, a.log_level.upper(), logging.INFO)
    setup_logging(a.log_json, level)
    try:
        cfg = _override_rigctld(load_config(a.config), a.rigctld)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if a.check_config:
        print(f"{a.config}: OK ({', '.join(cfg.enabled_radios())} enabled)")
        return 0
    broker = Broker.build(cfg, mock=a.mock, allow_tx=a.allow_tx, mock_services=a.mock_services)
    host = a.host or cfg.api.host
    port = a.port or cfg.api.port
    logging.getLogger("rigbroker").info("web UI on http://%s:%d/ (allow_tx=%s)", host, port,
                                        a.allow_tx)
    server = uvicorn.Server(uvicorn.Config(
        create_app(broker), host=host, port=port, log_config=None,
        log_level=logging.WARNING if level <= logging.INFO else level,
        access_log=False, ws_ping_interval=20, ws_ping_timeout=20,
    ))
    server.run()  # handles SIGINT/SIGTERM; lifespan stops the broker cleanly
    return 0


if __name__ == "__main__":
    sys.exit(main())
