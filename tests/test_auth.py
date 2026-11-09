"""Accounts & security: store, first run, forced change, roles, and every API access rule."""

from __future__ import annotations

import copy
import io
import os
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rigbroker.admin import main as admin_main
from rigbroker.api import create_app
from rigbroker.auth import COOKIE, INITIAL_FILE, AuthError, AuthStore, LoginLimiter
from rigbroker.broker import Broker
from tests.conftest import BASE, LOGIN, PASSWORD, auth_store, make_config

H = {"X-RigBroker": "1"}
NEW = "a much better passphrase"

# ---------------------------------------------------------------- store


def test_users_and_password_policy() -> None:
    s = AuthStore(None)
    assert not s.has_users()
    for bad, why in [("short", "at least 10"), ("aaaaaaaaaaaa", "repetitive"),
                     ("operator1x", "username")]:
        with pytest.raises(AuthError, match=why):
            s.add_user("operator1x" if why == "username" else "bob", bad)
    u, temp = s.add_user("Bob", None, "operator")
    assert u.name == "bob" and u.must_change and temp and len(temp) == 14
    assert s.check_login("BOB", temp) is not None
    assert s.check_login("bob", "wrong password") is None
    assert s.check_login("nobody", temp) is None
    with pytest.raises(AuthError, match="already exists"):
        s.add_user("bob")
    with pytest.raises(AuthError, match="username"):
        s.add_user("bad name!")
    with pytest.raises(AuthError, match="current password is wrong"):
        s.set_password("bob", NEW, current="nope")
    s.set_password("bob", NEW, current=temp)
    assert not s.get("bob").must_change
    with pytest.raises(AuthError, match="different"):
        s.set_password("bob", NEW, current=NEW)


def test_last_admin_is_protected() -> None:
    s = auth_store()
    with pytest.raises(AuthError, match="last admin"):
        s.update_user("admin", role="operator")
    with pytest.raises(AuthError, match="last admin"):
        s.update_user("admin", disabled=True)
    with pytest.raises(AuthError, match="last admin"):
        s.delete_user("admin")
    s.add_user("second", PASSWORD, "admin", must_change=False)
    s.update_user("admin", role="operator")  # fine now


def test_sessions_are_per_user_and_revocable() -> None:
    s = auth_store()
    s.add_user("bob", PASSWORD, "operator", must_change=False)
    admin, bob = s.get("admin"), s.get("bob")
    va, _ = s.make_session(admin, 1)
    vb, _ = s.make_session(bob, 1)
    assert s.check_session(va) is admin and s.check_session(vb) is bob
    forged = va.replace("admin.", "bob.", 1)
    assert s.check_session(forged) is None  # can't switch user without the key
    assert s.check_session(s.make_session(admin, -1)[0]) is None  # expired
    s.reset_password("bob")  # kills bob's sessions only
    assert s.check_session(vb) is None and s.check_session(va) is admin
    vb2, _ = s.make_session(bob, 1)
    s.update_user("bob", disabled=True)
    assert s.check_session(vb2) is None and s.check_login("bob", PASSWORD) is None
    s.delete_user("bob")
    s.logout_everyone()
    assert s.check_session(va) is None


def test_tokens_have_roles() -> None:
    s = AuthStore(None)
    info, plain = s.create_token("home-assistant")
    assert info.role == "operator" and plain.startswith("rb_") and plain not in str(s.tokens)
    assert s.check_token(plain) is info and s.check_token(plain + "x") is None
    _, adm = s.create_token("cli-admin", "admin")
    assert s.check_token(adm).role == "admin"  # type: ignore[union-attr]
    with pytest.raises(AuthError, match="already exists"):
        s.create_token("home-assistant")
    assert s.revoke_token(info.id) and s.check_token(plain) is None


def test_first_run_bootstrap(tmp_path: Path) -> None:
    f = tmp_path / "cfg" / "auth.json"
    s = AuthStore(str(f))
    temp = s.bootstrap_admin()
    assert temp and s.get("admin").must_change and s.get("admin").role == "admin"
    init = tmp_path / "cfg" / INITIAL_FILE
    assert init.read_text().strip() == temp
    if os.name == "posix":
        assert stat.S_IMODE(init.stat().st_mode) == 0o600
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert s.bootstrap_admin() is None  # only once
    s.set_password("admin", NEW, current=temp)
    assert not init.exists()  # one-time password file removed once a real one is set
    assert temp not in f.read_text() and NEW not in f.read_text()


def test_migrates_old_shared_password(tmp_path: Path) -> None:
    f = tmp_path / "auth.json"
    old = AuthStore(str(f))
    old.add_user("tmp", PASSWORD, "admin", must_change=False)
    data = yaml.safe_load(f.read_text())  # JSON is YAML
    import json
    legacy = {"session_secret": data["session_secret"], "tokens": {},
              "password": data["users"]["tmp"]["hash"]}
    f.write_text(json.dumps(legacy))
    s = AuthStore(str(f))
    assert s.check_login("admin", PASSWORD) is not None and s.get("admin").role == "admin"


def test_live_reload(tmp_path: Path) -> None:
    f = tmp_path / "auth.json"
    broker_view = AuthStore(str(f))
    time.sleep(0.01)
    AuthStore(str(f)).add_user("bob", PASSWORD, "operator", must_change=False)
    broker_view.reload_if_changed()
    assert broker_view.check_login("bob", PASSWORD) is not None


def test_limiter() -> None:
    lim = LoginLimiter(max_failures=3, lockout_s=60)
    for _ in range(3):
        lim.fail("k")
    assert lim.locked("k") > 50 and lim.locked("other") == 0
    lim.ok("k")
    assert lim.locked("k") == 0


# ---------------------------------------------------------------- API


@pytest.fixture
def anon() -> Iterator[tuple[TestClient, Broker, AuthStore]]:
    broker = Broker.build(make_config(), mock=True, allow_tx=True, simulate=False)
    store = auth_store()
    store.add_user("op", PASSWORD, "operator", must_change=False)
    with TestClient(create_app(broker, auth_store=store)) as c:
        end = time.monotonic() + 3
        while c.get("/radios/vu").json()["state"] != "default":
            assert time.monotonic() < end
            time.sleep(0.01)
        yield c, broker, store


def login(c: TestClient, user: str = "admin", pw: str = PASSWORD) -> int:
    return int(c.post("/auth/login", json={"username": user, "password": pw},
                      headers=H).status_code)


def test_view_is_public_control_is_not(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, _ = anon
    for path in ("/status", "/winlink/gateways", "/events", "/aprs", "/direwolf"):
        assert c.get(path).status_code == 200, path
    for method, path, body in [
        ("POST", "/radios/vu/lease", {"profile": "monitor"}),
        ("POST", "/radios/vu/release", None),
        ("POST", "/radios/vu/fault/clear", None),
        ("POST", "/aprs/messages", {"to": "W4X", "text": "hi"}),
        ("PUT", "/direwolf/settings", {"beacon": False}),
        ("GET", "/auth/users", None),
        ("POST", "/auth/users", {"name": "eve"}),
    ]:
        assert c.request(method, path, json=body, headers=H).status_code == 401, path
    me = c.get("/auth/me").json()
    assert me["authenticated"] is False and me["first_run"] is False


def test_login_by_username_and_holder(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, broker, _ = anon
    assert login(c, "op", "wrong password!") == 401
    assert login(c, "nobody") == 401
    assert login(c, "OP") == 200  # usernames are case-insensitive
    me = c.get("/auth/me").json()
    assert (me["name"], me["role"]) == ("op", "operator")
    r = c.post("/radios/vu/lease", json={"profile": "monitor", "holder": "spoofed"}, headers=H)
    assert r.status_code == 200 and r.json()["lease"]["holder"] == "op"
    audit = [e.message for e in broker.events.recent(0, kind="audit")]
    assert any("FAILED login for 'op'" in m for m in audit)
    assert any("POST /radios/vu/lease by op (session, operator)" in m for m in audit)
    c.post("/auth/logout", headers=H)
    assert c.post("/radios/vu/release", headers=H).status_code == 401


def test_roles(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, _ = anon
    assert login(c, "op") == 200
    assert c.post("/aprs/messages", json={"to": "W4X", "text": "hi"}, headers=H).status_code \
        in (200, 409)  # allowed for operators (409 only if off air)
    for method, path, body in [("PUT", "/direwolf/settings", {"beacon": False}),
                               ("POST", "/direwolf/apply", None),
                               ("GET", "/auth/users", None),
                               ("POST", "/winlink/gateways",
                                {"call": "W4NEW-1", "freq": 145030000, "mode": "packet"})]:
        r = c.request(method, path, json=body, headers=H)
        assert r.status_code == 403 and "admin role required" in r.json()["detail"], path
    c.post("/auth/logout", headers=H)
    assert login(c, "admin") == 200
    assert c.get("/auth/users", headers=H).status_code == 200


def test_forced_password_change(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, store = anon
    _, made = store.add_user("newbie", None, "operator")
    assert made is not None
    temp: str = made
    r = c.post("/auth/login", json={"username": "newbie", "password": temp}, headers=H)
    assert r.status_code == 200 and r.json()["must_change"] is True
    assert c.get("/auth/me").json()["must_change"] is True
    r = c.post("/radios/vu/release", headers=H)
    assert r.status_code == 403 and "password change required" in r.json()["detail"]
    assert c.post("/auth/password", json={"current": temp, "new": "short"},
                  headers=H).status_code == 422
    r = c.post("/auth/password", json={"current": temp, "new": NEW}, headers=H)
    assert r.status_code == 200
    assert c.get("/auth/me").json()["must_change"] is False  # still logged in
    assert c.post("/radios/vu/release", headers=H).status_code == 409  # authorized now
    assert login(c, "newbie", temp) == 401  # the one-time password is dead


def test_user_admin_api(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, _ = anon
    assert login(c) == 200
    r = c.post("/auth/users", json={"name": "Carol", "role": "operator"}, headers=H)
    assert r.status_code == 200 and r.json()["user"]["must_change"] is True
    temp = r.json()["temp_password"]
    names = [u["name"] for u in c.get("/auth/users", headers=H).json()["users"]]
    assert "carol" in names and "hash" not in str(c.get("/auth/users", headers=H).json())
    assert c.post("/auth/users", json={"name": "carol"}, headers=H).status_code == 422
    assert c.patch("/auth/users/carol", json={"role": "admin"}, headers=H).status_code == 200
    r = c.post("/auth/users/carol/reset", headers=H)
    assert r.status_code == 200 and r.json()["temp_password"] != temp
    assert c.patch("/auth/users/carol", json={"disabled": True}, headers=H).status_code == 200
    assert c.delete("/auth/users/admin", headers=H).status_code == 409  # not yourself
    assert c.delete("/auth/users/carol", headers=H).status_code == 200
    assert c.delete("/auth/users/carol", headers=H).status_code == 404
    assert c.patch("/auth/users/admin", json={"role": "operator"}, headers=H).status_code == 409


def test_cookie_flags(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, _ = anon
    r = c.post("/auth/login", json=LOGIN, headers=H)
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc and "max-age=43200" in sc
    assert c.cookies.get(COOKIE)


def test_lockout_per_address_and_user(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, _ = anon
    for _ in range(5):
        assert login(c, "op", "not the password") == 401
    r = c.post("/auth/login", json={"username": "op", "password": PASSWORD}, headers=H)
    assert r.status_code == 429 and "try again" in r.json()["detail"]


def test_csrf_protections(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, _ = anon
    assert c.post("/auth/login", json=LOGIN).status_code == 403
    assert c.post("/auth/login", json=LOGIN,
                  headers={**H, "Origin": "http://evil.example"}).status_code == 403
    assert login(c) == 200
    r = c.post("/radios/vu/release")
    assert r.status_code == 403 and "X-RigBroker" in r.json()["detail"]
    r = c.post("/radios/vu/release", headers={**H, "Origin": "http://evil.example"})
    assert r.status_code == 403 and "cross-origin" in r.json()["detail"]
    r = c.post("/auth/password", json={"current": PASSWORD, "new": NEW})
    assert r.status_code == 403
    r = c.post("/radios/vu/release", headers={**H, "Origin": "http://testserver"})
    assert r.status_code == 409


def test_token_auth(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, store = anon
    _, tok = store.create_token("home-assistant")
    auth = {"Authorization": f"Bearer {tok}"}
    r = c.post("/radios/vu/lease", json={"profile": "monitor"}, headers=auth)
    assert r.status_code == 200 and r.json()["lease"]["holder"] == "home-assistant"
    assert c.put("/direwolf/settings", json={"beacon": False}, headers=auth).status_code == 403
    bad = c.post("/radios/vu/release", headers={"Authorization": "Bearer rb_nope"})
    assert bad.status_code == 401 and "invalid API token" in bad.json()["detail"]


def test_first_run_through_the_app(tmp_path: Path) -> None:
    cfg = make_config(security={"auth_file": str(tmp_path / "auth.json")})
    broker = Broker.build(cfg, mock=True, allow_tx=True, simulate=False)
    with TestClient(create_app(broker, manage_lifecycle=False)) as c:
        assert c.get("/auth/me").json()["first_run"] is True
        temp = (tmp_path / INITIAL_FILE).read_text().strip()
        r = c.post("/auth/login", json={"username": "admin", "password": temp}, headers=H)
        assert r.json()["must_change"] is True
        assert c.post("/auth/password", json={"current": temp, "new": NEW},
                      headers=H).status_code == 200
        assert c.get("/auth/me").json()["first_run"] is False
        assert not (tmp_path / INITIAL_FILE).exists()
        assert c.get("/login").status_code == 200 and c.get("/account").status_code == 200


def test_private_view() -> None:
    cfg = make_config(security={"public_view": False})
    broker = Broker.build(cfg, mock=True, allow_tx=True, simulate=False)
    with TestClient(create_app(broker, auth_store=auth_store(), manage_lifecycle=False)) as c:
        assert c.get("/status").status_code == 401
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws") as ws:
            ws.receive_json()
        assert c.get("/").status_code == 200 and c.get("/login").status_code == 200
        assert login(c) == 200
        assert c.get("/status").status_code == 200


def test_websocket_origin_check(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, _ = anon
    with pytest.raises(WebSocketDisconnect), \
            c.websocket_connect("/ws", headers={"Origin": "http://evil.example"}) as ws:
        ws.receive_json()
    with c.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "status"


def test_security_headers(anon: tuple[TestClient, Broker, AuthStore]) -> None:
    c, _, _ = anon
    for page in ("/", "/login", "/account"):
        r = c.get(page)
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"], page
        assert r.headers["x-frame-options"] == "DENY"
    assert c.get("/static/app.js").headers["cache-control"] == "no-cache"
    web = Path(__file__).parent.parent / "web"
    for html in ("index.html", "login.html", "account.html"):
        text = (web / html).read_text()
        assert "style=" not in text and "<script>" not in text, html  # CSP: no inline


# ---------------------------------------------------------------- rigbroker-admin


def _config_file(tmp_path: Path) -> str:
    data = copy.deepcopy(BASE)
    data["security"] = {"auth_file": str(tmp_path / "auth.json")}
    f = tmp_path / "rb.yaml"
    f.write_text(yaml.safe_dump(data))
    return str(f)


def test_admin_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                   capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _config_file(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    assert admin_main(["--config", cfg, "passwd", "--stdin"]) == 0  # creates admin on 1st run
    st = AuthStore(str(tmp_path / "auth.json"))
    assert st.check_login("admin", PASSWORD) is not None and not st.get("admin").must_change
    assert not (tmp_path / INITIAL_FILE).exists()
    capsys.readouterr()
    assert admin_main(["--config", cfg, "user", "add", "dave", "--role", "operator"]) == 0
    temp = capsys.readouterr().out.strip().splitlines()[-1]
    assert AuthStore(str(tmp_path / "auth.json")).check_login("dave", temp) is not None
    assert admin_main(["--config", cfg, "user", "role", "dave", "admin"]) == 0
    assert admin_main(["--config", cfg, "user", "disable", "dave"]) == 0
    assert admin_main(["--config", cfg, "user", "list"]) == 0
    assert "dave" in capsys.readouterr().out
    assert admin_main(["--config", cfg, "user", "delete", "admin"]) == 1  # last enabled admin
    assert admin_main(["--config", cfg, "token", "create", "ha"]) == 0
    assert admin_main(["--config", cfg, "token", "revoke", "ha"]) == 0
    assert admin_main(["--config", cfg, "status"]) == 0
    assert "users:" in capsys.readouterr().out
