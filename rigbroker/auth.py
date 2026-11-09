"""Accounts and authentication (stdlib only).

* **Users** with a role: ``operator`` (runs the radio) or ``admin`` (also users + Direwolf).
  Passwords are scrypt hashes. New / reset accounts get a one-time password and must
  choose their own at first login (``must_change``).
* **First run**: if there are no users, an ``admin`` account is created with a random
  one-time password, written to ``initial-admin-password`` (mode 0600) next to the auth
  file. The file is deleted once admin sets a real password.
* **Sessions**: stateless HMAC-signed cookies naming the user and the user's session
  version; changing/resetting a password or disabling/deleting a user bumps the version,
  which kills that user's sessions everywhere.
* **API tokens** (Home Assistant, CLI): ``rb_`` + 256 random bits, stored as SHA-256,
  each with a role, individually revocable.
* **Lockout** per client address and per username.

The file is written atomically with mode 0600 and re-read when it changes on disk, so
``rigbroker-admin`` edits take effect without a restart.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

COOKIE = "rb_session"
CSRF_HEADER = "x-rigbroker"  # browsers can't send custom headers cross-site without CORS
TOKEN_PREFIX = "rb_"
MIN_PASSWORD = 10
INITIAL_FILE = "initial-admin-password"
Role = Literal["operator", "admin"]
ROLES: tuple[Role, ...] = ("operator", "admin")
_RANK = {"operator": 1, "admin": 2}
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}
_MAXMEM = 64 * 1024 * 1024
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")


class AuthError(Exception):
    """Refused account operation (message is user-readable)."""


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=32)


def _hash_password(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), **_SCRYPT, "hash": _scrypt(password, salt, **_SCRYPT).hex()}


def _verify(password: str, h: dict[str, Any]) -> bool:
    got = _scrypt(password, bytes.fromhex(h["salt"]), h["n"], h["r"], h["p"])
    return hmac.compare_digest(got.hex(), h["hash"])


_DUMMY = _hash_password(secrets.token_hex(8))  # keeps unknown-user logins as slow as real ones


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def temp_password() -> str:
    """Readable one-time password, e.g. 'kf3m-9xq2-p7wd' (~60 bits)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def normalize_username(name: str) -> str:
    n = name.strip().lower()
    if not _NAME_RE.match(n):
        raise AuthError("username: 2-32 chars, a-z 0-9 . _ - (starting with a letter/digit)")
    return n


def check_password_policy(password: str, username: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
    if password.lower() == username.lower():
        raise AuthError("password can't be the username")
    if len(set(password)) < 4:
        raise AuthError("password is too repetitive")


@dataclass
class User:
    name: str
    role: Role
    hash: dict[str, Any]
    must_change: bool = True
    disabled: bool = False
    session_ver: int = 1
    created: float = field(default_factory=time.time)
    last_login: float | None = None

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "role": self.role, "must_change": self.must_change,
                "disabled": self.disabled, "created": self.created,
                "last_login": self.last_login}


@dataclass
class TokenInfo:
    id: str
    name: str
    hash: str
    created: float
    role: Role = "operator"
    last_used: float | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "role": self.role, "created": self.created,
                "last_used": self.last_used}


@dataclass(frozen=True)
class Principal:
    """Who is making a request."""

    kind: str  # "session" | "token"
    name: str  # username or token name
    role: Role
    must_change: bool = False

    def can(self, role: Role) -> bool:
        return _RANK[self.role] >= _RANK[role]


class AuthStore:
    """Users, session key and API tokens, persisted in one 0600 JSON file."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(os.path.expanduser(path)) if path else None
        self.users: dict[str, User] = {}
        self.session_secret = secrets.token_bytes(32)
        self.tokens: dict[str, TokenInfo] = {}
        self._mtime: float | None = None
        self._load()

    # ---------------------------------------------------------------- persistence

    @property
    def initial_file(self) -> Path | None:
        return self.path.parent / INITIAL_FILE if self.path else None

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            if self.path is not None:
                self._save()
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.session_secret = bytes.fromhex(data["session_secret"])
        self.users = {k: User(**v) for k, v in data.get("users", {}).items()}
        self.tokens = {k: TokenInfo(**v) for k, v in data.get("tokens", {}).items()}
        if not self.users and data.get("password"):  # migrate the old shared password
            self.users["admin"] = User("admin", "admin", data["password"], must_change=False)
            self._save()
        self._mtime = self.path.stat().st_mtime

    def reload_if_changed(self) -> None:
        """Pick up edits made by ``rigbroker-admin`` while the broker runs."""
        if self.path is None:
            return
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if m != self._mtime:
            self._load()

    def _save(self) -> None:
        if self.path is None:
            return
        data = {"session_secret": self.session_secret.hex(),
                "users": {k: asdict(v) for k, v in self.users.items()},
                "tokens": {k: asdict(v) for k, v in self.tokens.items()}}
        self._write_private(self.path, json.dumps(data, indent=1))
        self._mtime = self.path.stat().st_mtime

    @staticmethod
    def _write_private(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".auth-")
        with contextlib.suppress(AttributeError, OSError):  # no fchmod on Windows dev boxes
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    # ---------------------------------------------------------------- first run

    def bootstrap_admin(self) -> str | None:
        """If there are no users: create ``admin`` with a one-time password, write it to
        the initial-password file and return it. Otherwise None."""
        if self.users:
            return None
        pw = temp_password()
        self.users["admin"] = User("admin", "admin", _hash_password(pw), must_change=True)
        self._save()
        if self.initial_file is not None:
            self._write_private(self.initial_file, pw + "\n")
        return pw

    def _initial_done(self) -> None:
        f = self.initial_file
        if f is not None:
            with contextlib.suppress(FileNotFoundError):
                f.unlink()

    # ---------------------------------------------------------------- users

    def has_users(self) -> bool:
        return bool(self.users)

    def get(self, name: str) -> User:
        u = self.users.get(name.strip().lower())
        if u is None:
            raise AuthError(f"no user {name!r}")
        return u

    def _admins(self) -> list[User]:
        return [u for u in self.users.values() if u.role == "admin" and not u.disabled]

    def add_user(self, name: str, password: str | None = None, role: Role = "operator",
                 *, must_change: bool = True) -> tuple[User, str | None]:
        """Create a user. Without ``password`` a one-time password is generated and
        returned (shown once). Raises AuthError."""
        n = normalize_username(name)
        if n in self.users:
            raise AuthError(f"user {n!r} already exists")
        if role not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        temp: str | None = None
        if password is None:
            password = temp = temp_password()
            must_change = True
        else:
            check_password_policy(password, n)
        self.users[n] = User(n, role, _hash_password(password), must_change=must_change)
        self._save()
        return self.users[n], temp

    def set_password(self, name: str, new: str, *, current: str | None = None) -> None:
        """Set a user's own password (``current`` required unless an admin tool). Logs that
        user out everywhere. Raises AuthError."""
        u = self.get(name)
        if current is not None and not _verify(current, u.hash):
            raise AuthError("current password is wrong")
        check_password_policy(new, u.name)
        if _verify(new, u.hash):
            raise AuthError("choose a password different from the current one")
        u.hash, u.must_change, u.session_ver = _hash_password(new), False, u.session_ver + 1
        self._save()
        if u.role == "admin":
            self._initial_done()

    def reset_password(self, name: str) -> str:
        """Admin reset: one-time password (returned once), must change, sessions killed."""
        u = self.get(name)
        temp = temp_password()
        u.hash, u.must_change, u.session_ver = _hash_password(temp), True, u.session_ver + 1
        self._save()
        return temp

    def update_user(self, name: str, *, role: Role | None = None,
                    disabled: bool | None = None) -> User:
        u = self.get(name)
        if role is not None and role not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        losing_admin = u.role == "admin" and not u.disabled and (
            (role is not None and role != "admin") or disabled is True)
        if losing_admin and len(self._admins()) <= 1:
            raise AuthError("can't remove the last admin")
        if role is not None:
            u.role = role
        if disabled is not None and disabled != u.disabled:
            u.disabled = disabled
            u.session_ver += 1
        self._save()
        return u

    def delete_user(self, name: str) -> None:
        u = self.get(name)
        if u.role == "admin" and not u.disabled and len(self._admins()) <= 1:
            raise AuthError("can't delete the last admin")
        del self.users[u.name]
        self._save()

    def check_login(self, name: str, password: str) -> User | None:
        """User on success; None for wrong password, unknown or disabled user (same
        timing in every case)."""
        u = self.users.get(name.strip().lower())
        ok = _verify(password, u.hash if u else _DUMMY)
        if u is None or not ok or u.disabled:
            return None
        u.last_login = time.time()
        self._save()
        return u

    # ---------------------------------------------------------------- sessions

    def make_session(self, user: User, hours: float) -> tuple[str, int]:
        exp = int(time.time() + hours * 3600)
        payload = f"{user.name}.{user.session_ver}.{exp}.{secrets.token_urlsafe(9)}"
        sig = hmac.new(self.session_secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}", exp

    def check_session(self, value: str | None) -> User | None:
        if not value:
            return None
        payload, _, sig = value.rpartition(".")
        good = hmac.new(self.session_secret, payload.encode(), hashlib.sha256).hexdigest()
        if not payload or not hmac.compare_digest(sig, good):
            return None
        try:
            name, ver, exp, _nonce = payload.rsplit(".", 3)
            if int(exp) <= time.time():
                return None
        except ValueError:
            return None
        u = self.users.get(name)
        if u is None or u.disabled or str(u.session_ver) != ver:
            return None
        return u

    def logout_everyone(self) -> None:
        self.session_secret = secrets.token_bytes(32)
        self._save()

    # ---------------------------------------------------------------- API tokens

    def create_token(self, name: str, role: Role = "operator") -> tuple[TokenInfo, str]:
        """Create a token; the plaintext is returned once and never stored."""
        name = name.strip()
        if not name or len(name) > 40 or not name.replace("-", "").replace("_", "").isalnum():
            raise AuthError("token name: 1-40 letters, digits, - or _")
        if any(t.name == name for t in self.tokens.values()):
            raise AuthError(f"a token named {name!r} already exists")
        if role not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
        info = TokenInfo(id=secrets.token_hex(4), name=name, hash=_sha256(plain),
                         created=time.time(), role=role)
        self.tokens[info.id] = info
        self._save()
        return info, plain

    def revoke_token(self, id_or_name: str) -> bool:
        for tid, t in list(self.tokens.items()):
            if id_or_name in (tid, t.name):
                del self.tokens[tid]
                self._save()
                return True
        return False

    def check_token(self, plain: str | None) -> TokenInfo | None:
        if not plain or not plain.startswith(TOKEN_PREFIX):
            return None
        h = _sha256(plain)
        for t in self.tokens.values():
            if hmac.compare_digest(t.hash, h):
                t.last_used = time.time()
                return t
        return None


class LoginLimiter:
    """Locks a key (address or username) after ``max_failures`` failures in ``window_s``."""

    def __init__(self, max_failures: int, lockout_s: float, window_s: float = 600) -> None:
        self.max_failures = max_failures
        self.lockout_s = lockout_s
        self.window_s = window_s
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked(self, key: str) -> float:
        return max(0.0, self._locked_until.get(key, 0.0) - time.monotonic())

    def fail(self, key: str) -> None:
        now = time.monotonic()
        fails = [t for t in self._fails.get(key, []) if now - t < self.window_s] + [now]
        self._fails[key] = fails
        if len(fails) >= self.max_failures:
            self._locked_until[key] = now + self.lockout_s
            self._fails[key] = []

    def ok(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked_until.pop(key, None)
