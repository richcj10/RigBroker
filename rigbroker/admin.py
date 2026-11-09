"""``rigbroker-admin``: local account administration (run on the Pi; edits the auth file).

  rigbroker-admin status
  rigbroker-admin passwd [USER]              set a password directly (default: admin)
  rigbroker-admin user list
  rigbroker-admin user add NAME [--role operator|admin]   prints a one-time password
  rigbroker-admin user reset NAME            new one-time password, logs them out
  rigbroker-admin user role NAME ROLE
  rigbroker-admin user disable|enable|delete NAME
  rigbroker-admin token create NAME [--role operator|admin]
  rigbroker-admin token list | token revoke NAME
  rigbroker-admin logout-all

Shell access to the Pi is what makes you the ultimate admin: this works even if every
web admin is locked out. The running broker picks up changes automatically.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time

from .auth import ROLES, AuthError, AuthStore
from .config import ConfigError, load_config


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rigbroker-admin", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/profiles.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    pw = sub.add_parser("passwd", help="set a user's password")
    pw.add_argument("user", nargs="?", default="admin")
    pw.add_argument("--stdin", action="store_true", help="read the password from stdin")
    us = sub.add_parser("user", help="manage users")
    usub = us.add_subparsers(dest="ucmd", required=True)
    usub.add_parser("list")
    ua = usub.add_parser("add")
    ua.add_argument("name")
    ua.add_argument("--role", choices=ROLES, default="operator")
    for c in ("reset", "disable", "enable", "delete"):
        usub.add_parser(c).add_argument("name")
    ur = usub.add_parser("role")
    ur.add_argument("name")
    ur.add_argument("role", choices=ROLES)
    tk = sub.add_parser("token", help="manage API tokens")
    tsub = tk.add_subparsers(dest="tcmd", required=True)
    tc = tsub.add_parser("create")
    tc.add_argument("name")
    tc.add_argument("--role", choices=ROLES, default="operator")
    tsub.add_parser("list")
    tsub.add_parser("revoke").add_argument("name_or_id")
    sub.add_parser("logout-all")
    a = p.parse_args(argv)

    try:
        cfg = load_config(a.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if cfg.security.auth_file is None:
        print("security.auth_file is null (memory only); nothing to administer", file=sys.stderr)
        return 2
    store = AuthStore(cfg.security.auth_file)

    try:
        if a.cmd == "status":
            print(f"auth file:   {store.path}")
            first = store.initial_file is not None and store.initial_file.exists()
            state = f"yes - one-time admin password in {store.initial_file}" if first else "done"
            print(f"first run:   {state}")
            print(f"users:       {len(store.users)}  |  API tokens: {len(store.tokens)}")
            print(f"public view: {cfg.security.public_view}")
        elif a.cmd == "passwd":
            if not store.users and a.user == "admin":
                store.bootstrap_admin()  # first run from the CLI: create admin, then set it
            if a.stdin:
                new = sys.stdin.readline().rstrip("\n")
            else:
                new = getpass.getpass(f"New password for {a.user}: ")
                if getpass.getpass("Repeat: ") != new:
                    print("passwords don't match", file=sys.stderr)
                    return 1
            store.set_password(a.user, new)
            print(f"password for {a.user} set; their other sessions are logged out")
        elif a.cmd == "user":
            return _user(store, a)
        elif a.cmd == "token" and a.tcmd == "create":
            info, plain = store.create_token(a.name, a.role)
            print(f"token {info.name} ({info.role}, id {info.id}) created. "
                  "Copy it now; it is not shown again:")
            print(plain)
        elif a.cmd == "token" and a.tcmd == "list":
            if not store.tokens:
                print("no API tokens")
            for t in store.tokens.values():
                print(f"{t.id}  {t.name:<24} {t.role:<9} created {_when(t.created)}")
        elif a.cmd == "token" and a.tcmd == "revoke":
            if not store.revoke_token(a.name_or_id):
                print(f"no token {a.name_or_id!r}", file=sys.stderr)
                return 1
            print(f"revoked {a.name_or_id}")
        elif a.cmd == "logout-all":
            store.logout_everyone()
            print("all browser sessions invalidated")
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _user(store: AuthStore, a: argparse.Namespace) -> int:
    if a.ucmd == "list":
        if not store.users:
            print("no users (the broker creates 'admin' on first start)")
        for u in store.users.values():
            flags = ", ".join(f for f, on in (("disabled", u.disabled),
                                              ("must set password", u.must_change)) if on)
            print(f"{u.name:<20} {u.role:<9} last login {_when(u.last_login)}"
                  + (f"  [{flags}]" if flags else ""))
    elif a.ucmd == "add":
        u, temp = store.add_user(a.name, None, a.role)
        print(f"user {u.name} ({u.role}) created. One-time password (must be changed at "
              f"first login):\n{temp}")
    elif a.ucmd == "reset":
        print(f"one-time password for {a.name}:\n{store.reset_password(a.name)}")
    elif a.ucmd == "role":
        store.update_user(a.name, role=a.role)
        print(f"{a.name} is now {a.role}")
    elif a.ucmd in ("disable", "enable"):
        store.update_user(a.name, disabled=a.ucmd == "disable")
        print(f"{a.name} {a.ucmd}d")
    elif a.ucmd == "delete":
        store.delete_user(a.name)
        print(f"{a.name} deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
