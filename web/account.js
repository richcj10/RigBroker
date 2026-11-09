"use strict";
// Account page: own password; admins manage users. All user data rendered as text.
const $ = (s) => document.querySelector(s);
const H = { "Content-Type": "application/json", "X-RigBroker": "1" };
let me = null;

async function call(method, path, body) {
  const r = await fetch(path, { method, credentials: "same-origin", headers: H, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  const d = data.detail || data.reason;
  const msg = Array.isArray(d) ? d.map((e) => e.msg).join("; ") : d;
  if (r.status === 401) location.href = "/login?next=/account";
  return { ok: r.ok, data, msg: msg || r.statusText };
}
let toastTimer = 0;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 3000);
}
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "never");
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

async function init() {
  me = await (await fetch("/auth/me", { credentials: "same-origin" })).json();
  if (!me.authenticated) { location.href = "/login?next=/account"; return; }
  if (me.must_change) { location.href = "/login?next=/account"; return; }
  $("#who").textContent = `${me.name} · ${me.role}`;
  $("#pw-form").username.value = me.name;
  $("#admin").hidden = me.role !== "admin";
  if (me.role === "admin") loadUsers();
}

function showTemp(user, pw) {
  $("#temp-user").textContent = user;
  $("#temp-pw").textContent = pw;
  $("#temp").hidden = false;
}

async function loadUsers() {
  const r = await call("GET", "/auth/users");
  if (!r.ok) return;
  const body = document.createDocumentFragment();
  for (const u of r.data.users) {
    const tr = el("tr");
    const name = el("td", "call");
    name.append(el("b", null, u.name));
    if (u.name === me.name) name.append(el("span", "tag", "you"));
    const roleTd = el("td");
    const sel = el("select");
    for (const r of ["operator", "admin"]) { const o = el("option", null, r); o.value = r; o.selected = r === u.role; sel.append(o); }
    sel.addEventListener("change", async () => {
      const res = await call("PATCH", `/auth/users/${encodeURIComponent(u.name)}`, { role: sel.value });
      toast(res.ok ? `${u.name} is now ${sel.value}` : res.msg, !res.ok);
      loadUsers();
    });
    roleTd.append(sel);
    const status = u.disabled ? "disabled" : u.must_change ? "must set password" : "active";
    const acts = el("div", "acts");
    const reset = el("button", "ghost small", "Reset password");
    reset.type = "button";
    reset.addEventListener("click", async () => {
      if (!confirm(`Reset ${u.name}'s password? They'll be logged out and get a one-time password.`)) return;
      const res = await call("POST", `/auth/users/${encodeURIComponent(u.name)}/reset`);
      if (res.ok) showTemp(u.name, res.data.temp_password); else toast(res.msg, true);
      loadUsers();
    });
    const dis = el("button", "ghost small", u.disabled ? "Enable" : "Disable");
    dis.type = "button";
    dis.addEventListener("click", async () => {
      const res = await call("PATCH", `/auth/users/${encodeURIComponent(u.name)}`, { disabled: !u.disabled });
      toast(res.ok ? `${u.name} ${u.disabled ? "enabled" : "disabled"}` : res.msg, !res.ok);
      loadUsers();
    });
    acts.append(reset, dis);
    if (u.name !== me.name) {
      const del = el("button", "ghost small", "Delete");
      del.type = "button";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete user ${u.name}?`)) return;
        const res = await call("DELETE", `/auth/users/${encodeURIComponent(u.name)}`);
        toast(res.ok ? `${u.name} deleted` : res.msg, !res.ok);
        loadUsers();
      });
      acts.append(del);
    }
    const actTd = el("td");
    actTd.append(acts);
    tr.append(name, roleTd, el("td", u.disabled ? "sub" : "", status), el("td", "sub", when(u.last_login)), actTd);
    body.append(tr);
  }
  $("#users").replaceChildren(body);
  const toks = document.createDocumentFragment();
  for (const t of r.data.tokens) toks.append(el("li", null, `${t.name} (${t.role}) · created ${when(t.created)} · last used ${when(t.last_used)}`));
  if (!r.data.tokens.length) toks.append(el("li", "sub", "none"));
  $("#tokens").replaceChildren(toks);
}

$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("#pw-err").textContent = "";
  if (f.new.value !== f.repeat.value) { $("#pw-err").textContent = "The new passwords don't match."; return; }
  const r = await call("POST", "/auth/password", { current: f.current.value, new: f.new.value });
  if (!r.ok) { $("#pw-err").textContent = r.msg; return; }
  f.current.value = f.new.value = f.repeat.value = "";
  toast("Password changed");
});

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const r = await call("POST", "/auth/users", { name: f.name.value.trim(), role: f.role.value });
  if (!r.ok) { toast(r.msg, true); return; }
  showTemp(r.data.user.name, r.data.temp_password);
  f.reset();
  loadUsers();
});

$("#temp-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#temp-pw").textContent); toast("Copied"); }
  catch { toast("Copy failed: select the text instead", true); }
});
$("#temp-hide").addEventListener("click", () => { $("#temp").hidden = true; $("#temp-pw").textContent = ""; });
$("#logout").addEventListener("click", async () => {
  await call("POST", "/auth/logout", {});
  location.href = "/";
});

init();
