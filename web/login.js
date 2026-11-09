"use strict";
// Login page: sign in, forced password change, redirect back.
const $ = (s) => document.querySelector(s);
const H = { "Content-Type": "application/json", "X-RigBroker": "1" };

async function post(path, body) {
  const r = await fetch(path, { method: "POST", credentials: "same-origin", headers: H, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  const d = data.detail || data.reason;
  const msg = Array.isArray(d) ? d.map((e) => e.msg).join("; ") : d;
  return { ok: r.ok, status: r.status, data, msg: msg || r.statusText };
}

// Only same-site relative targets: never redirect to another origin.
function nextUrl() {
  const n = new URLSearchParams(location.search).get("next") || "/";
  return n.startsWith("/") && !n.startsWith("//") && !n.startsWith("/\\") ? n : "/";
}

let currentPassword = "";

function show(which) {
  for (const id of ["login-form", "change-form", "signed-in"]) $("#" + id).hidden = id !== which;
  const f = $("#" + which).querySelector("input:not([hidden])");
  if (f) f.focus();
}

async function refresh() {
  const me = await (await fetch("/auth/me", { credentials: "same-origin" })).json();
  $("#first-run").hidden = !me.first_run || me.authenticated;
  if (me.initial_file) {
    // files under /var/lib/rigbroker belong to the service user
    const sudo = me.initial_file.startsWith("/var/lib/") ? "sudo " : "";
    $("#first-run-cmd").textContent = `${sudo}cat ${me.initial_file}`;
  }
  if (!me.authenticated) return show("login-form");
  if (me.must_change) {
    $("#change-form").username.value = me.name;
    return show("change-form");
  }
  $("#me-name").textContent = me.name;
  $("#me-role").textContent = me.role;
  show("signed-in");
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("#login-err").textContent = "";
  const r = await post("/auth/login", { username: f.username.value.trim(), password: f.password.value });
  if (!r.ok) { $("#login-err").textContent = r.msg; f.password.select(); return; }
  currentPassword = f.password.value;
  f.password.value = "";
  if (r.data.must_change) {
    $("#change-form").username.value = r.data.name;
    return show("change-form");
  }
  location.href = nextUrl();
});

$("#change-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("#change-err").textContent = "";
  if (f.new.value !== f.repeat.value) { $("#change-err").textContent = "The passwords don't match."; return; }
  if (!currentPassword) {  // page reloaded mid-change: sign in again first
    $("#change-err").textContent = "Please log in again first.";
    return show("login-form");
  }
  const r = await post("/auth/password", { current: currentPassword, new: f.new.value });
  if (!r.ok) { $("#change-err").textContent = r.msg; return; }
  currentPassword = "";
  f.reset();
  location.href = nextUrl();
});

$("#logout").addEventListener("click", async () => {
  await post("/auth/logout", {});
  refresh();
});

refresh();
