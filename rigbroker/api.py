"""REST API, WebSocket push and static web UI (FastAPI).

Status codes: 200 ok, 202 queued, 401 login required, 403 refused (CSRF / no password set),
404 unknown radio, 409 conflict (with ``reason``), 422 invalid request, 429 login lockout.

Access: GET endpoints and the WebSocket are public when ``security.public_view`` is true;
everything that changes state needs the station-password session (plus the
``X-RigBroker: 1`` header) or an ``Authorization: Bearer rb_...`` API token.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from . import __version__
from .aprs import AprsError, AprsService, validate_text
from .auth import COOKIE, CSRF_HEADER, AuthError, AuthStore, LoginLimiter, Principal, Role, User
from .broker import Broker
from .config import WinlinkMode, normalize_call
from .direwolf import INSTANCES, SERVICE_PREFIX, DirewolfManager, SettingsError
from .leases import Verdict
from .radio import LeaseResult, RadioController

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# Events that change what the UI shows -> push a fresh status right away.
_STATUS_EVENTS = {"transition", "lease_started", "lease_ended", "lease_queued", "lease_dropped",
                  "step", "tx_inhibit", "rig_offline", "rig_online", "alert", "mock_inject",
                  "plan_next", "winlink_attempt", "winlink_gateway"}
# Leaflet is pinned with SRI from unpkg; map tiles come from OpenStreetMap. Nothing else
# external is allowed.
CSP = ("default-src 'self'; script-src 'self' https://unpkg.com; "
       "style-src 'self' https://unpkg.com; "
       "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordRequest(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=1, max_length=256)


class NewUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: Role = "operator"


class UpdateUserRequest(BaseModel):
    role: Role | None = None
    disabled: bool | None = None


class LeaseRequest(BaseModel):
    profile: str
    minutes: float | None = Field(default=None, gt=0)
    priority: str = "manual"
    holder: str = Field(default="web", min_length=1, max_length=64)
    queue: bool = False


class HolderRequest(BaseModel):
    holder: str = Field(default="web", min_length=1, max_length=64)
    priority: str = "manual"


class WinlinkCheckRequest(BaseModel):
    holder: str = Field(default="web", min_length=1, max_length=64)
    priority: str = "manual"
    gateway: str | None = Field(default=None, description="try only this gateway")


class FlagRequest(BaseModel):
    value: bool = True


class GatewayRequest(BaseModel):
    call: str
    freq: int = Field(gt=0)
    mode: WinlinkMode
    grid: str | None = None
    note: str = Field(default="", max_length=200)
    favorite: bool = True


class InjectRequest(BaseModel):
    fault: str


class AprsSendRequest(BaseModel):
    to: str = Field(min_length=1, max_length=12)
    text: str = Field(min_length=1, max_length=200)


def _lease_response(r: LeaseResult) -> JSONResponse:
    body = {"verdict": r.verdict.value, "reason": r.reason,
            "lease": r.lease.to_dict() if r.lease else None}
    if r.ok:
        return JSONResponse(body, status_code=202 if r.verdict is Verdict.QUEUE else 200)
    return JSONResponse(body, status_code=422 if r.invalid else 409)


def create_app(broker: Broker, *, manage_lifecycle: bool = True,
               auth_store: AuthStore | None = None) -> FastAPI:
    """Build the app. With ``manage_lifecycle`` the broker starts/stops with the server."""

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            await broker.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await broker.stop()

    app = FastAPI(title="RigBroker", version=__version__, lifespan=lifespan)
    interval = broker.config.api.status_interval_s

    # ---------------------------------------------------------------- security
    sec = broker.config.security
    store = auth_store if auth_store is not None else AuthStore(sec.auth_file)
    limiter = LoginLimiter(sec.max_failures, sec.lockout_minutes * 60)
    app.state.auth = store

    def client_ip(conn: HTTPConnection) -> str:
        return conn.client.host if conn.client else "?"

    def same_origin(conn: HTTPConnection) -> bool:
        """Browsers always send Origin cross-site; it must match our Host."""
        origin = conn.headers.get("origin")
        return origin is None or urlsplit(origin).netloc == conn.headers.get("host")

    if sec.auth_file and store.path is not None:
        pw = store.bootstrap_admin()
        if pw is not None:
            broker.events.emit("audit", "first run: created user 'admin' with a one-time "
                               f"password in {store.initial_file} (must be changed at login)",
                               level="warning")

    def identify(conn: HTTPConnection) -> Principal | None:
        store.reload_if_changed()
        authz = conn.headers.get("authorization", "")
        if authz[:7].lower() == "bearer ":
            tok = store.check_token(authz[7:].strip())
            return Principal("token", tok.name, tok.role) if tok else None
        u = store.check_session(conn.cookies.get(COOKIE))
        return Principal("session", u.name, u.role, u.must_change) if u else None

    def csrf_check(request: Request) -> None:
        # A cross-site page can't add this header without a CORS preflight, which we never
        # grant; and browsers always send Origin cross-site, which must match our Host.
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "missing X-RigBroker header")
        if not same_origin(request):
            raise HTTPException(403, "cross-origin request refused")

    def authorize(request: Request, role: Role) -> Principal:
        who = identify(request)
        if who is None:
            if request.headers.get("authorization"):
                raise HTTPException(401, "invalid API token")
            raise HTTPException(401, "login required")
        if who.kind == "session":
            csrf_check(request)
            if who.must_change:
                raise HTTPException(403, "password change required")
        if not who.can(role):
            raise HTTPException(403, f"{role} role required")
        broker.events.emit("audit", f"{request.method} {request.url.path} by {who.name} "
                           f"({who.kind}, {who.role}) from {client_ip(request)}",
                           who=who.name, via=who.kind, ip=client_ip(request))
        return who

    async def require_control(request: Request) -> Principal:
        return authorize(request, "operator")

    async def require_admin(request: Request) -> Principal:
        return authorize(request, "admin")

    async def require_view(request: Request) -> None:
        if not sec.public_view and identify(request) is None:
            raise HTTPException(401, "login required")

    view = APIRouter(dependencies=[Depends(require_view)])
    control = APIRouter(dependencies=[Depends(require_control)])
    admin = APIRouter(dependencies=[Depends(require_admin)])

    @app.middleware("http")
    async def security_headers(request: Request,
                               call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        # OSM's tile policy needs a Referer; send only our origin, never paths.
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
            resp.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/static/"):
            # always revalidate (cheap 304 via ETag) so an update is never hidden by a
            # stale cached app.js
            resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    def set_session(resp: Response, request: Request, user: User) -> None:
        value, _ = store.make_session(user, sec.session_hours)
        resp.set_cookie(COOKIE, value, max_age=int(sec.session_hours * 3600), httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")

    @app.post("/auth/login")
    async def login(req: LoginRequest, request: Request) -> JSONResponse:
        csrf_check(request)
        ip = client_ip(request)
        name = req.username.strip().lower()
        store.reload_if_changed()
        wait = max(limiter.locked(ip), limiter.locked(f"user:{name}"))
        if wait:
            raise HTTPException(429, f"too many failed logins; try again in {wait:.0f}s")
        user = await asyncio.to_thread(store.check_login, name, req.password)
        if user is None:
            limiter.fail(ip)
            limiter.fail(f"user:{name}")
            broker.events.emit("audit", f"FAILED login for {name!r} from {ip}",
                               level="warning", ip=ip)
            raise HTTPException(401, "wrong username or password")
        limiter.ok(ip)
        limiter.ok(f"user:{name}")
        resp = JSONResponse({"ok": True, "name": user.name, "role": user.role,
                             "must_change": user.must_change})
        set_session(resp, request, user)
        broker.events.emit("audit", f"login: {user.name} ({user.role}) from {ip}", ip=ip)
        return resp

    @app.post("/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        who = identify(request)
        if who is not None:
            broker.events.emit("audit", f"logout: {who.name} from {client_ip(request)}")
        return resp

    @app.get("/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        who = identify(request)
        return {"authenticated": who is not None, "via": who.kind if who else None,
                "name": who.name if who else None, "role": who.role if who else None,
                "must_change": who.must_change if who else False,
                "public_view": sec.public_view,
                "first_run": bool(store.initial_file and store.initial_file.exists()),
                "initial_file": str(store.initial_file) if store.initial_file else None}

    @app.post("/auth/password")
    async def change_password(req: PasswordRequest, request: Request) -> JSONResponse:
        """Change your own password (also clears must_change). Keeps you logged in."""
        csrf_check(request)
        who = identify(request)
        if who is None or who.kind != "session":
            raise HTTPException(401, "login required")
        ip = client_ip(request)
        if limiter.locked(f"user:{who.name}"):
            raise HTTPException(429, "too many failed attempts; try again later")
        try:
            await asyncio.to_thread(store.set_password, who.name, req.new, current=req.current)
        except AuthError as exc:
            if "current password" in str(exc):
                limiter.fail(f"user:{who.name}")
            raise HTTPException(422, str(exc)) from None
        resp = JSONResponse({"ok": True})
        set_session(resp, request, store.get(who.name))  # old sessions die; keep this one
        broker.events.emit("audit", f"{who.name} changed their password from {ip}", ip=ip)
        return resp

    # ---------------------------------------------------------------- user admin

    @admin.get("/auth/users")
    async def list_users() -> dict[str, Any]:
        return {"users": [u.public() for u in store.users.values()],
                "tokens": [t.public() for t in store.tokens.values()]}

    @admin.post("/auth/users")
    async def add_user(req: NewUserRequest,
                       who: Principal = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            u, temp = await asyncio.to_thread(store.add_user, req.name, None, req.role)
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from None
        broker.events.emit("audit", f"{who.name} added user {u.name} ({u.role})")
        return {"ok": True, "user": u.public(), "temp_password": temp}

    @admin.post("/auth/users/{name}/reset")
    async def reset_user(name: str,
                         who: Principal = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            temp = await asyncio.to_thread(store.reset_password, name)
        except AuthError as exc:
            raise HTTPException(404, str(exc)) from None
        broker.events.emit("audit", f"{who.name} reset the password of {name}")
        return {"ok": True, "temp_password": temp}

    @admin.patch("/auth/users/{name}")
    async def update_user(name: str, req: UpdateUserRequest,
                          who: Principal = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            u = store.update_user(name, role=req.role, disabled=req.disabled)
        except AuthError as exc:
            raise HTTPException(404 if "no user" in str(exc) else 409, str(exc)) from None
        broker.events.emit("audit", f"{who.name} updated user {u.name}: role={u.role} "
                           f"disabled={u.disabled}")
        return {"ok": True, "user": u.public()}

    @admin.delete("/auth/users/{name}")
    async def delete_user(name: str,
                          who: Principal = Depends(require_admin),  # noqa: B008
    ) -> dict[str, Any]:
        if name.strip().lower() == who.name:
            raise HTTPException(409, "you can't delete your own account")
        try:
            store.delete_user(name)
        except AuthError as exc:
            raise HTTPException(404 if "no user" in str(exc) else 409, str(exc)) from None
        broker.events.emit("audit", f"{who.name} deleted user {name}")
        return {"ok": True}

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return FileResponse(WEB_DIR / "login.html", headers={"Cache-Control": "no-cache"})

    @app.get("/account", include_in_schema=False)
    async def account_page() -> FileResponse:
        return FileResponse(WEB_DIR / "account.html", headers={"Cache-Control": "no-cache"})

    def radio(rid: str) -> RadioController:
        r = broker.radios.get(rid)
        if r is None:
            raise HTTPException(404, f"unknown radio {rid!r}")
        return r

    @view.get("/status")
    async def status() -> dict[str, Any]:
        return broker.status()

    @view.get("/radios/{rid}")
    async def get_radio(rid: str) -> dict[str, Any]:
        return radio(rid).status()

    @control.post("/radios/{rid}/lease")
    async def lease(rid: str, req: LeaseRequest,
                    who: Principal = Depends(require_control),  # noqa: B008
    ) -> JSONResponse:
        holder = who.name  # the logged-in user / token, never client-supplied
        r = await radio(rid).request_lease(req.profile, holder=holder,
                                           priority=req.priority, minutes=req.minutes,
                                           queue=req.queue)
        return _lease_response(r)

    @control.post("/radios/{rid}/release")
    async def release(rid: str, req: HolderRequest | None = None,
                      who: Principal = Depends(require_control),  # noqa: B008
    ) -> JSONResponse:
        by = who.name
        ok, msg = await radio(rid).release(by=by)
        return JSONResponse({"ok": ok, "reason": msg}, status_code=200 if ok else 409)

    @control.post("/radios/{rid}/winlink/check")
    async def winlink_check(rid: str, req: WinlinkCheckRequest | None = None,
                            who: Principal = Depends(require_control),  # noqa: B008
    ) -> JSONResponse:
        radio(rid)
        h = req or WinlinkCheckRequest()
        holder = who.name
        try:
            res = await broker.winlink_check(rid, holder=holder, priority=h.priority,
                                             gateway=h.gateway)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return _lease_response(res)

    # ---------------------------------------------------------------- winlink gateways

    @view.get("/winlink/gateways")
    async def gateways(radio_id: str | None = Query(None, alias="radio")) -> dict[str, Any]:
        """All known gateways ranked for a radio's winlink_profile (best first)."""
        prof = None
        if radio_id is not None:
            r = radio(radio_id)
            if r.cfg.winlink_profile:
                prof = r.cfg.profiles[r.cfg.winlink_profile]
        p = broker.planner
        return {"grid": p.cfg.grid, "period": p.day_or_night(),
                "gateways": [x.to_dict() for x in p.rank(prof)]}

    @admin.post("/winlink/gateways")
    async def add_gateway(req: GatewayRequest) -> dict[str, Any]:
        try:
            gw = broker.planner.add_gateway(req.call, req.freq, req.mode, req.grid, req.note,
                                            req.favorite)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        broker.events.emit("winlink_gateway", f"added gateway {gw.call} {gw.freq} Hz {gw.mode}")
        return {"ok": True, "call": gw.call}

    @admin.delete("/winlink/gateways/{call}")
    async def remove_gateway(call: str) -> dict[str, Any]:
        try:
            broker.planner.remove_gateway(call)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from None
        broker.events.emit("winlink_gateway", f"removed gateway {call.upper()}")
        return {"ok": True}

    @control.post("/winlink/gateways/{call}/favorite")
    async def favorite(call: str, req: FlagRequest) -> dict[str, Any]:
        try:
            broker.planner.set_favorite(call, req.value)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from None
        broker.events.emit("winlink_gateway",
                           f"{call.upper()} {'marked favorite' if req.value else 'unfavorited'}")
        return {"ok": True, "favorite": req.value}

    @control.post("/winlink/gateways/{call}/block")
    async def block(call: str, req: FlagRequest) -> dict[str, Any]:
        try:
            broker.planner.set_blocked(call, req.value)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from None
        broker.events.emit("winlink_gateway",
                           f"{call.upper()} {'blocked' if req.value else 'unblocked'}")
        return {"ok": True, "blocked": req.value}

    @control.post("/radios/{rid}/fault/clear")
    async def fault_clear(rid: str) -> JSONResponse:
        ok, msg = await radio(rid).clear_fault(by="web")
        return JSONResponse({"ok": ok, "reason": msg}, status_code=200 if ok else 409)

    @view.get("/events")
    async def events(limit: int = Query(100, ge=1, le=1000), radio_id: str | None = Query(
            None, alias="radio"), debug: bool = False) -> list[dict[str, Any]]:
        evs = broker.events.recent(0, radio=radio_id)
        if not debug:
            evs = [e for e in evs if e.level != "debug"]
        return [e.to_dict() for e in evs[-limit:]]

    @view.get("/schedule")
    async def schedule() -> list[dict[str, Any]]:
        return []

    @control.post("/mock/{rid}/inject")
    async def mock_inject(rid: str, req: InjectRequest) -> dict[str, str]:
        radio(rid)
        if not broker.mock:
            raise HTTPException(404, "fault injection is only available in --mock mode")
        try:
            return {"ok": "true", "message": broker.inject(rid, req.fault)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    # ---------------------------------------------------------------- APRS

    def aprs() -> AprsService:
        if broker.aprs is None:
            raise HTTPException(404, "APRS is not configured (add an aprs: section)")
        return broker.aprs

    @view.get("/aprs")
    async def aprs_snapshot() -> dict[str, Any]:
        return aprs().snapshot()

    @view.get("/aprs/stations")
    async def aprs_stations() -> list[dict[str, Any]]:
        return aprs().station_list()

    @view.get("/aprs/messages")
    async def aprs_messages() -> list[dict[str, Any]]:
        return aprs().message_list()

    @view.get("/aprs/packets")
    async def aprs_packets(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return [asdict(p) for p in list(aprs().packets)[-limit:]]

    @control.post("/aprs/messages")
    async def aprs_send(req: AprsSendRequest) -> JSONResponse:
        svc = aprs()
        try:  # bad input is 422; "can't transmit right now" is 409
            normalize_call(req.to)
            validate_text(req.text)
        except (ValueError, AprsError) as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            m = await svc.send_message(req.to, req.text)
        except AprsError as exc:
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, "message": asdict(m)})

    @control.post("/aprs/messages/{mid}/cancel")
    async def aprs_cancel(mid: str) -> dict[str, Any]:
        try:
            return {"ok": True, "message": asdict(await aprs().cancel(mid))}
        except AprsError as exc:
            raise HTTPException(404, str(exc)) from None

    # ---------------------------------------------------------------- Direwolf

    def direwolf() -> DirewolfManager:
        if broker.direwolf is None:
            raise HTTPException(404, "Direwolf is managed only with an aprs: section")
        return broker.direwolf

    @view.get("/direwolf")
    async def direwolf_info() -> dict[str, Any]:
        dw = direwolf()
        return {"installed": dw.installed(), "mock": broker.mock, "callsign": dw.callsign,
                "conf_dir": str(dw.conf_dir), "last_written": dw.last_written,
                "instances": list(INSTANCES), "settings": dw.public_settings(),
                "running": {r.id: r.consumer for r in broker.radios.values()
                            if r.consumer and r.consumer.startswith(SERVICE_PREFIX)}}

    @view.get("/direwolf/preview/{instance}", response_class=PlainTextResponse)
    async def direwolf_preview(instance: str) -> str:
        if instance not in INSTANCES:
            raise HTTPException(404, f"instance must be one of {', '.join(INSTANCES)}")
        texts = direwolf().render_all(mask_secrets=True)
        return next(t for i, t in texts.items() if i == instance)

    @admin.put("/direwolf/settings")
    async def direwolf_settings(changes: dict[str, Any]) -> dict[str, Any]:
        dw = direwolf()
        try:
            dw.update(changes)
        except SettingsError as exc:
            raise HTTPException(422, str(exc)) from None
        broker.events.emit("direwolf", "Direwolf settings changed: "
                           + ", ".join(sorted(k for k in changes if k != "passcode"))
                           + (" (+passcode)" if "passcode" in changes else ""))
        return {"ok": True, "settings": dw.public_settings()}

    @admin.post("/direwolf/apply")
    async def direwolf_apply() -> dict[str, Any]:
        direwolf()
        return {"ok": True, **(await broker.apply_direwolf())}

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        # Cross-site WebSocket hijacking: browsers send Origin; it must be ours.
        if not same_origin(sock) or (not sec.public_view and identify(sock) is None):
            broker.events.emit("audit", f"refused WebSocket from {client_ip(sock)} "
                               f"(origin {sock.headers.get('origin')})", level="warning")
            await sock.close(code=1008)
            return
        await sock.accept()
        q = broker.events.subscribe()
        aq = broker.aprs.subscribe() if broker.aprs else None
        dirty = asyncio.Event()
        try:
            await sock.send_json({"type": "status", "data": broker.status()})
            history = [e.to_dict() for e in broker.events.recent(200) if e.level != "debug"]
            await sock.send_json({"type": "events", "data": history})

            async def pump_events() -> None:
                while True:
                    ev = await q.get()
                    if ev.level == "debug":
                        continue
                    await sock.send_json({"type": "event", "data": ev.to_dict()})
                    if ev.kind in _STATUS_EVENTS:
                        dirty.set()

            async def pump_status() -> None:
                while True:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(dirty.wait(), timeout=interval)
                    dirty.clear()
                    await sock.send_json({"type": "status", "data": broker.status()})
                    await asyncio.sleep(0.1)  # coalesce bursts

            async def drain() -> None:
                while True:
                    await sock.receive_text()  # raises on disconnect

            async def pump_aprs() -> None:
                if aq is None:
                    await asyncio.Event().wait()
                    return
                while True:
                    upd = await aq.get()
                    await sock.send_json({"type": "aprs", **upd})

            tasks = [asyncio.create_task(c())
                     for c in (pump_events, pump_status, drain, pump_aprs)]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            broker.events.unsubscribe(q)
            if aq is not None and broker.aprs is not None:
                broker.aprs.unsubscribe(aq)

    app.include_router(view)
    app.include_router(control)
    app.include_router(admin)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
