"""rigbrokerctl against the real app via TestClient (no network)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rigbroker.api import create_app
from rigbroker.auth import AuthStore
from rigbroker.broker import Broker
from rigbroker.cli import Requester, find_token, main
from tests.conftest import make_config


@pytest.fixture
def req() -> Iterator[Requester]:
    broker = Broker.build(make_config(), mock=True, allow_tx=True, simulate=False)
    store = AuthStore(None)
    _, token = store.create_token("cli")
    with TestClient(create_app(broker, auth_store=store),
                    headers={"Authorization": f"Bearer {token}"}) as c:
        def _req(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
            r = c.request(method, path, json=body)
            return r.status_code, r.json()

        end = time.monotonic() + 3
        while _req("GET", "/radios/vu", None)[1]["state"] != "default":
            assert time.monotonic() < end
            time.sleep(0.01)
        yield _req


def test_status(req: Requester, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"], req) == 0
    out = capsys.readouterr().out
    assert "[vu] ID-4100A  state=DEFAULT  profile=aprs" in out
    assert "consumer: direwolf@aprs" in out and "[hf] not managed" in out


def test_lease_conflict_release(req: Requester, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lease", "vu", "monitor", "--minutes", "5"], req) == 0
    assert "granted" in capsys.readouterr().out
    end = time.monotonic() + 3
    while req("GET", "/radios/vu", None)[1]["state"] != "active":
        assert time.monotonic() < end
        time.sleep(0.01)
    # same token = same holder (can't be spoofed), so conflict on priority instead
    assert main(["lease", "vu", "vara_fm", "--priority", "scheduled"], req) == 1
    assert "error 409" in capsys.readouterr().err
    assert main(["release", "vu"], req) == 0
    assert main(["events", "--limit", "3"], req) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) >= 3


def test_gateway_commands(req: Requester, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gateways"], req) == 0
    out = capsys.readouterr().out
    assert "* W4FAV-10" in out and "grid=EM85ab" in out
    assert main(["fav", "k4far-10"], req) == 0
    assert main(["block", "W4FAV-10"], req) == 0
    capsys.readouterr()
    assert main(["gateways"], req) == 0
    out = capsys.readouterr().out
    assert "* K4FAR-10" in out and "x W4FAV-10" in out
    assert main(["fav", "NOPE-1"], req) == 1
    assert main(["winlink", "vu", "--gateway", "K4FAR-10"], req) == 0
    assert "granted" in capsys.readouterr().out


def test_json_output(req: Requester, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "status"], req) == 0
    assert json.loads(capsys.readouterr().out)["radios"]["vu"]["state"] == "default"


def test_token_holder_is_token_name(req: Requester) -> None:
    assert main(["lease", "vu", "monitor", "--holder", "someone-else"], req) == 0
    assert req("GET", "/radios/vu", None)[1]["lease"]["holder"] == "cli"


def test_find_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("RIGBROKER_TOKEN", raising=False)
    assert find_token(None) is None
    (tmp_path / ".config" / "rigbroker").mkdir(parents=True)
    (tmp_path / ".config" / "rigbroker" / "token").write_text("rb_file\n")
    assert find_token(None) == "rb_file"
    monkeypatch.setenv("RIGBROKER_TOKEN", "rb_env")
    assert find_token(None) == "rb_env"
    assert find_token("rb_arg") == "rb_arg"


def test_unreachable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--url", "http://127.0.0.1:9", "status"]) == 3
    assert "cannot reach rigbroker" in capsys.readouterr().err
