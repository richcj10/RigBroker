"use strict";
// RigBroker web UI: one WebSocket for live status + events, REST for commands.

const $ = (sel, root = document) => root.querySelector(sel);
const cards = new Map();          // radio id -> {el, f: {field: element}}
let status = null;
let statusAt = 0;                 // performance.now() when status arrived
let logRows = [];
const MAX_LOG = 400;

// ---------------------------------------------------------------- formatting
function fmtFreq(hz) {
  if (!hz) return "—";
  const s = String(Math.round(hz)).padStart(7, "0");
  return `${s.slice(0, -6)}.${s.slice(-6, -3)}.${s.slice(-3)}`;
}
function sUnits(db) {                 // Hamlib STRENGTH: dB relative to S9
  if (db == null) return "—";
  if (db > 0) return `S9+${Math.round(db)}`;
  return `S${Math.max(0, Math.min(9, Math.round(9 + db / 6)))}`;
}
function meterPct(db) {               // S0 (-54 dB) .. S9+60 -> 0..100 %, S9 at ~47 %
  if (db == null) return 0;
  const v = db <= 0 ? ((db + 54) / 54) * 47 : 47 + (db / 60) * 53;
  return Math.max(0, Math.min(100, v));
}
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), r = s % 60;
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m` : `${m}:${String(r).padStart(2, "0")}`;
}
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });

// ---------------------------------------------------------------- toast
let toastTimer = 0;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 3000);
}

// ---------------------------------------------------------------- REST
function errText(data, fallback) {
  const d = data.reason || data.detail || data.message || fallback;
  if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
  return typeof d === "string" ? d : JSON.stringify(d);
}
async function api(method, path, body, { quiet = false } = {}) {
  try {
    const r = await fetch(path, {
      method, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-RigBroker": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) { loadAuth(); openLogin(); }
    if (!r.ok) toast(`${r.status}: ${errText(data, r.statusText)}`, true);
    else if (!quiet) toast(data.verdict ? `${data.verdict}: ${data.reason}` : errText(data, "ok"));
    return { ok: r.ok, data };
  } catch (e) {
    toast(`request failed: ${e}`, true);
    return { ok: false };
  }
}

// ---------------------------------------------------------------- radio cards
function buildCard(id, r) {
  const el = $("#radio-tpl").content.firstElementChild.cloneNode(true);
  const f = {};
  el.querySelectorAll("[data-f]").forEach((n) => (f[n.dataset.f] = n));
  f.title.textContent = `${r.name}`;
  // profile buttons are static per config
  for (const [name, p] of Object.entries(r.profiles)) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "prof";
    b.dataset.profile = name;
    const isDefault = name === r.default_profile;
    const freq = p.rig_power === "off" ? "rig off" : `${fmtFreq(p.freq)} ${p.mode || ""}`;
    b.innerHTML = `<b></b><span></span>`;
    b.querySelector("b").textContent = name + (isDefault ? " (default)" : "");
    b.querySelector("span").textContent = freq;
    if (p.transmits) {
      const tx = document.createElement("span");
      tx.className = "txbadge";
      tx.textContent = `TX${p.max_minutes ? ` · ≤${p.max_minutes} min` : ""}${p.winlink_mode ? " · best gateway" : ""}`;
      b.append(tx);
    }
    if (isDefault) b.title = "Default profile: use Release to return to it";
    if (p.winlink_mode) {
      b.querySelector("span").textContent = `planner · ${p.winlink_mode}`;
      b.title = "Tries your best-ranked gateways in order (favorites first)";
    }
    b.addEventListener("click", () => {
      if (isDefault) return api("POST", `/radios/${id}/release`, { holder: "web" });
      if (p.winlink_mode) {
        return api("POST", `/radios/${id}/winlink/check`, { holder: "web", priority: f.priority.value });
      }
      const minutes = parseFloat(f.minutes.value);
      api("POST", `/radios/${id}/lease`, {
        profile: name, holder: "web", priority: f.priority.value, queue: f.queue.checked,
        minutes: Number.isFinite(minutes) && minutes > 0 ? minutes : null,
      });
    });
    f.profiles.append(b);
  }
  el.querySelector('[data-a="release"]').addEventListener("click", () => api("POST", `/radios/${id}/release`, { holder: "web" }));
  el.querySelector('[data-a="winlink"]').addEventListener("click", () => api("POST", `/radios/${id}/winlink/check`, { holder: "web" }));
  el.querySelector('[data-a="clear-fault"]').addEventListener("click", () => api("POST", `/radios/${id}/fault/clear`));
  el.querySelectorAll("[data-inject]").forEach((b) =>
    b.addEventListener("click", () => api("POST", `/mock/${id}/inject`, { fault: b.dataset.inject })));
  $("#radios").append(el);
  const card = { el, f };
  cards.set(id, card);
  return card;
}

function setChip(el, text, cls) {
  el.textContent = text;
  el.className = "chip" + (cls ? " " + cls : "");
}

function updateCard(id, r, mock) {
  const { f } = cards.get(id) || buildCard(id, r);
  const t = r.telemetry;
  const prof = r.applied_profile ? r.profiles[r.applied_profile] : null;

  f.sub.textContent = `${id} · default: ${r.default_profile}`;
  f.state.textContent = r.state === "boot" ? "waiting for rig" : r.state;
  f.state.className = `state ${r.state}`;

  const fault = r.fault;
  f.fault.hidden = !fault;
  if (fault) {
    f["fault-title"].textContent = `FAULT — TX inhibited${fault.latched ? " (latched: manual clear required)" : ""}`;
    f["fault-reason"].textContent = fault.reason;
    f["fault-meta"].textContent = `${fault.kind} at ${fmtTime(fault.ts)} · recovery attempts: ${fault.recovery_attempts}`;
  }

  const live = t.online && t.polling;
  f.freq.textContent = live ? fmtFreq(t.freq) : (t.polling ? "offline" : "rig off");
  f.mode.textContent = live ? (t.mode || "") : "";
  f.tx.classList.toggle("on", !!(live && t.ptt));
  f.busy.classList.toggle("on", !!(live && t.dcd));
  f.smeter.style.width = `${live ? meterPct(t.strength) : 0}%`;
  f.smeter.style.setProperty("--full", `${10000 / Math.max(1, meterPct(t.strength))}%`);
  f.sval.textContent = live ? sUnits(t.strength) : "—";

  let profText = r.applied_profile || "—";
  if (r.target_profile && r.target_profile !== r.applied_profile) profText += ` → ${r.target_profile}`;
  const plan = r.lease && r.lease.plan && r.lease.plan.length ? r.lease.plan : null;
  if (plan) {
    const step = plan[r.lease.attempt];
    profText += ` via ${step.label} (${r.lease.attempt + 1}/${plan.length})`;
  }
  f.profile.textContent = profText;
  if (r.applied_freq && live && t.freq !== r.applied_freq) f.freq.title = `verified ${fmtFreq(r.applied_freq)}`;
  f.power.textContent = prof && prof.power != null ? `${Math.round(prof.power * 100)}%` : "—";
  f.sql.textContent = prof && prof.squelch != null ? `${Math.round(prof.squelch * 100)}%` : "—";
  f.lease.dataset.remaining = r.lease && r.lease.remaining_s != null ? r.lease.remaining_s : "";
  f.lease.dataset.holder = r.lease ? `${r.lease.holder} (${r.lease.priority})` : "";

  setChip(f["h-civ"], t.polling ? (t.online ? "CI-V ok" : `CI-V offline${t.failures ? ` ×${t.failures}` : ""}`) : "CI-V idle (rig off)",
    t.polling ? (t.online ? "ok" : "bad") : "");
  if (f["h-civ"]) f["h-civ"].title = t.last_error || (t.last_ok ? `last ok ${fmtTime(t.last_ok)}` : "");
  setChip(f["h-svc"], r.consumer ? `${r.consumer} running` : "no consumer", r.consumer ? "ok" : "");
  setChip(f["h-tx"], r.allow_tx ? (r.fault ? "TX inhibited (fault)" : "TX allowed") : "TX disabled (--allow-tx off)",
    r.allow_tx && !r.fault ? "ok" : "warn");

  f.profiles.querySelectorAll(".prof").forEach((b) => {
    const name = b.dataset.profile;
    b.classList.toggle("current", name === r.applied_profile && r.state !== "switching");
    b.classList.toggle("target", name === r.target_profile && name !== r.applied_profile);
    const why = name === r.default_profile ? null : r.profiles[name].unavailable;
    const busy = r.state === "switching" || r.state === "returning";
    b.disabled = r.state === "fault" || r.state === "boot" || busy || !!why;
    b.title = why ? `Unavailable: ${why}` : busy ? "Wait: the radio is switching" : "";
    b.classList.toggle("unavail", !!why);
  });
  const rel = f.profiles.closest(".radio").querySelector('[data-a="release"]');
  rel.disabled = !r.lease;
  const wl = f.profiles.closest(".radio").querySelector('[data-a="winlink"]');
  wl.hidden = !r.winlink_profile;
  // no run job = the Winlink client is on another computer (Winlink Express over the LAN)
  if (r.winlink_profile) {
    wl.textContent = r.profiles[r.winlink_profile].run ? "Winlink check" : "Start Winlink session";
  }
  const wlWhy = r.winlink_profile ? r.profiles[r.winlink_profile].unavailable : null;
  wl.disabled = r.state === "fault" || !!wlWhy || r.state === "switching" || r.state === "returning";
  wl.title = wlWhy ? `Unavailable: ${wlWhy}` : "";

  f.queue.hidden = !r.queue.length;
  f.queue.textContent = r.queue.length
    ? "Queued: " + r.queue.map((q) => `${q.profile} (${q.holder}, ${q.priority})`).join(" · ") : "";
  f.mock.hidden = !mock;
}

function tickCountdowns() {
  const dt = (performance.now() - statusAt) / 1000;
  for (const { f } of cards.values()) {
    const rem = f.lease.dataset.remaining;
    f.lease.textContent = rem === "" ? "—" : `${fmtDur(parseFloat(rem) - dt)} · ${f.lease.dataset.holder}`;
  }
}

function render() {
  if (!status) return;
  $("#mode").textContent = status.mock ? "MOCK" : "HARDWARE";
  $("#mode").className = "pill " + (status.mock ? "warn" : "ok");
  $("#txmode").textContent = status.allow_tx ? "allow-tx" : "TX disabled";
  $("#txmode").className = "pill " + (status.allow_tx ? "ok" : "warn");
  for (const [id, r] of Object.entries(status.radios)) updateCard(id, r, status.mock);
  if (window.onStatus) window.onStatus(status);
  winlinkHelpState(status);
  const sched = $("#schedule");
  sched.replaceChildren();
  const items = status.schedule.length ? status.schedule.map((s) => JSON.stringify(s))
    : ["No schedules yet (scheduler arrives in milestone 5)."];
  for (const s of items) { const li = document.createElement("li"); li.textContent = s; sched.append(li); }
  for (const rid of status.disabled_radios || []) {
    if (!cards.has(rid)) {
      const el = document.createElement("article");
      el.className = "radio";
      el.innerHTML = `<header class="radio-head"><div><h2></h2><div class="sub">not managed (no profiles configured)</div></div><span class="state boot">disabled</span></header>`;
      el.querySelector("h2").textContent = rid;
      $("#radios").append(el);
      cards.set(rid, { el, f: { lease: { dataset: { remaining: "" } } } });
    }
  }
  tickCountdowns();
}

// ---------------------------------------------------------------- winlink gateways
let gwRadio = null;      // radio whose winlink_profile decides usability
let gwLoading = false;
const GW_EVENTS = new Set(["winlink_attempt", "winlink_gateway", "plan_next", "lease_started", "lease_ended"]);

function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

async function loadGateways() {
  if (gwLoading) return;
  gwLoading = true;
  try {
    const q = gwRadio ? `?radio=${encodeURIComponent(gwRadio)}` : "";
    const r = await fetch(`/winlink/gateways${q}`);
    if (r.ok) renderGateways(await r.json());
  } finally { gwLoading = false; }
}

function trying() {
  const r = status && gwRadio ? status.radios[gwRadio] : null;
  const l = r && r.lease;
  return l && l.plan && l.plan.length ? l.plan[l.attempt].key : null;
}

function renderGateways(data) {
  const per = data.period ? ` · it's ${data.period} at your location` : "";
  $("#gw-info").textContent = data.grid
    ? `Your grid ${data.grid}${per}. Favorites first, then success rate, then distance.`
    : "Set winlink.grid in the config to rank by distance and day/night. Favorites first, then success rate.";
  const body = $("#gw-body");
  const frag = document.createDocumentFragment();
  const now = trying();
  if (!data.gateways.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="9" class="sub">No gateways yet — add the ones you know work.</td>`;
    frag.append(tr);
  }
  for (const g of data.gateways) {
    const tr = document.createElement("tr");
    tr.className = [g.usable ? "" : "unusable", g.blocked ? "blocked" : "", g.call === now ? "trying" : ""].join(" ");
    const st = g.stats;
    const rate = st.attempts ? st.successes / st.attempts : null;
    const dots = st.recent.slice(-6).map((ok) => `<span class="${ok ? "ok" : "no"}">${ok ? "●" : "○"}</span>`).join("");
    tr.innerHTML = `
      <td><button type="button" class="star${g.favorite ? " on" : ""}" aria-label="favorite" title="${g.favorite ? "Unfavorite" : "Mark as known-good favorite"}">${g.favorite ? "★" : "☆"}</button></td>
      <td class="call"><b></b>${g.source === "user" ? '<span class="tag">added</span>' : ""}${g.blocked ? '<span class="tag">blocked</span>' : ""}${g.call === now ? '<span class="tag">trying now</span>' : ""}<span class="note"></span></td>
      <td class="num">${fmtFreq(g.freq)}</td>
      <td>${g.mode}${g.band ? ` <span class="sub">${g.band}</span>` : ""}</td>
      <td class="num">${g.distance_km == null ? "—" : g.distance_km + " km"}</td>
      <td><span class="rate">${rate == null ? '<span class="sub">untried</span>' : `<span class="rate-bar"><i></i></span>${st.successes}/${st.attempts}`}</span> <span class="dots">${dots}</span></td>
      <td class="sub">${ago(st.last_ok)}</td>
      <td class="num">${g.score}</td>
      <td><div class="acts">
        <button type="button" class="ghost small" data-gw="try" ${g.usable && !g.blocked ? "" : "disabled"}>Try</button>
        <button type="button" class="ghost small" data-gw="block">${g.blocked ? "Unblock" : "Block"}</button>
        ${g.source === "user" ? '<button type="button" class="ghost small" data-gw="remove">Remove</button>' : ""}
      </div></td>`;
    const bar = tr.querySelector(".rate-bar i");
    if (bar) bar.style.width = `${Math.round(rate * 100)}%`;  // CSSOM: allowed by the CSP
    tr.querySelector("b").textContent = g.call;
    tr.querySelector(".note").textContent = [g.note, !g.usable ? g.reasons[g.reasons.length - 1] : ""].filter(Boolean).join(" — ");
    tr.title = "Score: " + g.reasons.join(", ");
    const call = encodeURIComponent(g.call);
    tr.querySelector(".star").addEventListener("click", async () => {
      await api("POST", `/winlink/gateways/${call}/favorite`, { value: !g.favorite }, { quiet: true });
      loadGateways();
    });
    tr.querySelector('[data-gw="try"]').addEventListener("click", () =>
      api("POST", `/radios/${encodeURIComponent(gwRadio)}/winlink/check`, { holder: "web", gateway: g.call }));
    tr.querySelector('[data-gw="block"]').addEventListener("click", async () => {
      await api("POST", `/winlink/gateways/${call}/block`, { value: !g.blocked });
      loadGateways();
    });
    const rm = tr.querySelector('[data-gw="remove"]');
    if (rm) rm.addEventListener("click", async () => {
      if (!confirm(`Remove ${g.call}? Its history is kept.`)) return;
      await api("DELETE", `/winlink/gateways/${call}`);
      loadGateways();
    });
    frag.append(tr);
  }
  body.replaceChildren(frag);
}

$("#gw-add-toggle").addEventListener("click", () => {
  const form = $("#gw-form");
  form.hidden = !form.hidden;
  if (!form.hidden) form.call.focus();
});
$("#gw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fm = e.target;
  const res = await api("POST", "/winlink/gateways", {
    call: fm.call.value.trim(), freq: Math.round(parseFloat(fm.mhz.value) * 1e6), mode: fm.mode.value,
    grid: fm.grid.value.trim() || null, note: fm.note.value.trim(), favorite: fm.favorite.checked,
  });
  if (res.ok) { fm.reset(); fm.favorite.checked = true; fm.hidden = true; loadGateways(); }
});

// ---------------------------------------------------------------- event log
function addEvents(evs) {
  logRows.push(...evs);
  if (logRows.length > MAX_LOG) logRows = logRows.slice(-MAX_LOG);
  renderLog();
}
function renderLog() {
  const showSteps = $("#f-steps").checked, warnOnly = $("#f-warn").checked;
  const body = $("#log");
  const frag = document.createDocumentFragment();
  for (let i = logRows.length - 1; i >= 0; i--) {
    const e = logRows[i];
    if (!showSteps && e.kind === "step") continue;
    if (warnOnly && (e.level === "info" || e.level === "debug")) continue;
    const tr = document.createElement("tr");
    tr.className = e.level;
    for (const v of [fmtTime(e.ts), e.level, e.radio || "-", `${e.kind}: ${e.message}`]) {
      const td = document.createElement("td");
      td.textContent = v;
      tr.append(td);
    }
    frag.append(tr);
  }
  body.replaceChildren(frag);
}
$("#f-steps").addEventListener("change", renderLog);
$("#f-warn").addEventListener("change", renderLog);
$("#log-clear").addEventListener("click", () => { logRows = []; renderLog(); });

// ---------------------------------------------------------------- login
let auth = { authenticated: false };
async function loadAuth() {
  try {
    const r = await fetch("/auth/me", { credentials: "same-origin" });
    if (r.ok) auth = await r.json();
  } catch { /* offline: keep last state */ }
  if (auth.authenticated && auth.must_change) return openLogin();  // finish password setup
  const st = $("#auth-state"), btn = $("#auth-btn");
  if (auth.authenticated) {
    st.textContent = auth.via === "token" ? `token: ${auth.name}` : `${auth.name} · ${auth.role}`;
    st.className = "pill ok";
    st.title = "Account settings";
    btn.textContent = "Log out";
    btn.hidden = auth.via === "token";
  } else {
    st.textContent = "view only";
    st.className = "pill";
    st.title = "";
    btn.textContent = "Log in";
    btn.hidden = false;
  }
}
function openLogin() {
  location.href = `/login?next=${encodeURIComponent("/" + location.hash)}`;
}
$("#auth-btn").addEventListener("click", async () => {
  if (!auth.authenticated) return openLogin();
  await fetch("/auth/logout", { method: "POST", credentials: "same-origin", headers: { "X-RigBroker": "1" } });
  toast("logged out");
  loadAuth();
});

// ---------------------------------------------------------------- websocket
let backoff = 500;
function setConn(on, text) {
  $("#conn-dot").className = "dot " + (on ? "on" : "off");
  $("#conn-text").textContent = text;
}
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { backoff = 500; setConn(true, "live"); if (window.aprsInit) window.aprsInit(); };
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "status") {
      const first = !status;
      status = msg.data; statusAt = performance.now(); render();
      if (first) {
        gwRadio = Object.keys(status.radios).find((k) => status.radios[k].winlink_profile) || null;
        loadGateways();
      }
    }
    else if (msg.type === "aprs") { if (window.onAprs) window.onAprs(msg); }
    else if (msg.type === "events") { logRows = []; addEvents(msg.data); }
    else if (msg.type === "event") {
      addEvents([msg.data]);
      if (msg.data.level === "alert") toast(msg.data.message, true);
      if (GW_EVENTS.has(msg.data.kind)) loadGateways();
    }
  };
  ws.onclose = () => {
    setConn(false, `disconnected — retrying in ${Math.round(backoff / 1000)}s`);
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10000);
  };
  ws.onerror = () => ws.close();
}
// ---------------------------------------------------------------- Winlink Express help
// Fill in the real address/ports so the instructions match this Pi.
async function fillWinlinkHelp() {
  document.querySelectorAll('[data-wx="host"]').forEach((e) => (e.textContent = location.hostname));
  try {
    const r = await fetch("/direwolf", { credentials: "same-origin" });
    if (r.ok) {
      const d = await r.json();
      document.querySelectorAll('[data-wx="pkt"]').forEach((e) => (e.textContent = String(d.settings.pkt_kiss_port)));
    }
  } catch { /* keep defaults */ }
}
function winlinkHelpState(st) {
  const el = document.querySelector('[data-wx="state"]');
  if (!el || !st) return;
  const r = Object.values(st.radios).find((x) => x.winlink_profile);
  if (!r) return;
  const why = r.profiles[r.winlink_profile].unavailable || (r.profiles.vara_fm && r.profiles.vara_fm.unavailable);
  el.textContent = why ? `Right now: Winlink sessions are unavailable (${why}).`
    : r.lease ? `Right now: ${r.lease.profile} session active for ${r.lease.holder}.` : "Right now: ready - start a session above.";
}
fillWinlinkHelp();

// ---------------------------------------------------------------- views (#station / #aprs)
const VIEWS = ["station", "aprs"];
function showView() {
  let v = location.hash.replace("#", "");
  if (!VIEWS.includes(v) || (v === "aprs" && $("#nav-aprs").hidden && window.aprsKnown === false)) v = "station";
  document.body.dataset.view = v;
  document.querySelectorAll(".views a").forEach((a) => a.classList.toggle("on", a.dataset.view === v));
  document.title = v === "aprs" ? "APRS · RigBroker" : "RigBroker";
  if (window.onViewChange) window.onViewChange(v);
}
window.addEventListener("hashchange", showView);
showView();

setInterval(tickCountdowns, 250);
setInterval(loadGateways, 60000);  // day/night and "last OK" ages drift slowly
setInterval(loadAuth, 60000);      // notice session expiry
loadAuth();
connect();
