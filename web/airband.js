"use strict";
// Airband tab: receive-only AM listening on the VHF radio, audio played in the browser.
(() => {
  const A = { radio: null, profile: null, step: 25000, range: [118000000, 136975000], named: new Map(), r: null };
  const RATE = 16000;
  const au = { ctx: null, gain: null, ws: null, next: 0, want: false, frames: 0 };

  const mhz = (hz) => (hz / 1e6).toFixed(3);
  async function tune(hz) {
    const res = await api("POST", `/radios/${A.radio}/tune`, { freq: Math.round(hz), mode: "AM" }, { quiet: true });
    if (!res.ok && res.data && res.data.reason) toast(res.data.reason, true);
  }

  // ------------------------------------------------------------ channel list + grid
  function buildNamed(channels) {
    A.named = new Map(channels.map((c) => [c.freq, c]));
    const groups = new Map();
    for (const c of channels) {
      const g = c.group || "Other";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(c);
    }
    const frag = document.createDocumentFragment();
    for (const [g, list] of groups) {
      const box = document.createElement("div");
      box.className = "ab-group";
      const h = document.createElement("b");
      h.textContent = g;
      box.append(h);
      for (const c of list) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "small ab-ch"; b.dataset.freq = c.freq;
        const n = document.createElement("span"); n.textContent = c.name;
        const f = document.createElement("small"); f.textContent = mhz(c.freq);
        b.append(n, f);
        b.title = `${g} ${c.name} ${mhz(c.freq)} MHz${c.source === "user" ? " (added here)" : ""}`;
        b.addEventListener("click", () => tune(c.freq));
        box.append(b);
      }
      frag.append(box);
    }
    $("#ab-named").replaceChildren(frag);
  }
  function buildGrid() {
    const [lo, hi] = A.range;
    const frag = document.createDocumentFragment();
    for (let base = Math.floor(lo / 1e6) * 1e6; base <= hi; base += 1e6) {
      const row = document.createElement("div");
      row.className = "ab-row";
      const lab = document.createElement("span");
      lab.className = "ab-mhz"; lab.textContent = String(base / 1e6);
      row.append(lab);
      for (let f = base; f < base + 1e6; f += A.step) {
        if (f < lo || f > hi) continue;
        const c = document.createElement("button");
        c.type = "button"; c.className = "ab-cell"; c.dataset.freq = f;
        c.textContent = String((f - base) / 1000).padStart(3, "0");
        const named = A.named.get(f);
        if (named) { c.classList.add("named"); c.title = `${mhz(f)} ${named.group} ${named.name}`; }
        else c.title = `${mhz(f)} MHz`;
        c.addEventListener("click", () => tune(f));
        row.append(c);
      }
      frag.append(row);
    }
    $("#ab-grid").replaceChildren(frag);
  }
  async function load() {
    let r;
    try { r = await fetch("/airband"); } catch { return; }
    if (!r.ok) return;  // no airband section
    const d = await r.json();
    A.radio = d.radio; A.profile = d.profile; A.step = d.step_hz; A.range = d.range;
    $("#nav-airband").hidden = false;
    $("#airband-panel").hidden = false;
    buildNamed(d.channels);
    buildGrid();
    if (A.r) render(A.r);
  }

  // ------------------------------------------------------------ audio (Web Audio API)
  function playChunk(buf) {
    const pcm = new Int16Array(buf);
    const ab = au.ctx.createBuffer(1, pcm.length, RATE);
    const ch = ab.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const src = au.ctx.createBufferSource();
    src.buffer = ab;
    src.connect(au.gain);
    const now = au.ctx.currentTime;
    if (au.next < now + 0.05 || au.next > now + 1.5) au.next = now + 0.3;  // (re)start the jitter buffer
    src.start(au.next);
    au.next += ab.duration;
    au.frames++;
  }
  function openAudio() {
    if (!au.want) return;
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/audio`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { $("#ab-audio-state").textContent = "playing"; };
    ws.onmessage = (m) => { if (au.ctx) playChunk(m.data); };
    ws.onclose = () => {
      if (au.ws === ws) au.ws = null;
      if (au.want) { $("#ab-audio-state").textContent = "reconnecting…"; setTimeout(openAudio, 1500); }
    };
    au.ws = ws;
  }
  async function startAudio() {
    if (!au.ctx) {
      au.ctx = new AudioContext();
      au.gain = au.ctx.createGain();
      au.gain.gain.value = parseFloat($("#ab-vol").value);
      au.gain.connect(au.ctx.destination);
    }
    await au.ctx.resume();
    au.want = true;
    au.next = 0;
    if (!au.ws) openAudio();
    $("#ab-play").textContent = "■ Mute";
  }
  function stopAudio() {
    au.want = false;
    if (au.ws) { au.ws.close(); au.ws = null; }
    if (au.ctx) au.ctx.suspend();
    $("#ab-play").textContent = "▶ Play audio";
    $("#ab-audio-state").textContent = "";
  }
  window.airbandAudio = au;  // for diagnostics

  // ------------------------------------------------------------ controls
  $("#ab-play").addEventListener("click", () => (au.want ? stopAudio() : startAudio()));
  $("#ab-vol").addEventListener("input", () => { if (au.gain) au.gain.gain.value = parseFloat($("#ab-vol").value); });
  $("#ab-start").addEventListener("click", () => {
    const r = A.r;
    if (r && r.applied_profile === A.profile && r.lease) {
      stopAudio();
      api("POST", `/radios/${A.radio}/release`, { holder: "web" });
    } else api("POST", `/radios/${A.radio}/lease`, { profile: A.profile, holder: "web" });
  });
  const sql = (d) => {
    const cur = A.r && A.r.squelch != null ? A.r.squelch : 0.25;
    const v = Math.max(0, Math.min(1, Math.round((cur + d) * 20) / 20));
    api("POST", `/radios/${A.radio}/squelch`, { level: v }, { quiet: true }).then((res) => {
      if (!res.ok && res.data && res.data.reason) toast(res.data.reason, true);
    });
  };
  $("#ab-sql-down").addEventListener("click", () => sql(-0.05));
  $("#ab-sql-up").addEventListener("click", () => sql(0.05));
  $("#ab-freq-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = parseFloat(e.target.mhz.value);
    if (Number.isFinite(v)) tune(Math.round(v * 1e6 / A.step) * A.step);
  });
  $("#ab-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const res = await api("POST", "/airband/channels", {
      freq: Math.round(parseFloat(f.mhz.value) * 1e6 / A.step) * A.step, name: f.name.value.trim(), group: f.group.value.trim().toUpperCase(),
    });
    if (res.ok) { f.reset(); load(); }
  });

  // ------------------------------------------------------------ status
  function render(r) {
    A.r = r;
    const on = r.applied_profile === A.profile && r.state === "active";
    const t = r.telemetry;
    $("#ab-start").textContent = r.lease && r.applied_profile === A.profile ? "Stop (back to APRS)" : "Start listening";
    $("#ab-start").className = r.lease && r.applied_profile === A.profile ? "danger" : "";
    $("#ab-start").disabled = r.state === "switching" || r.state === "returning" || !!r.fault
      || (!!r.lease && r.applied_profile !== A.profile);
    $("#ab-sub").textContent = r.fault ? `FAULT: ${r.fault.reason}`
      : on ? `Listening (${r.lease ? r.lease.holder : "?"}) - APRS is off until you stop.`
      : r.lease && r.applied_profile !== A.profile ? `Radio busy: ${r.applied_profile} for ${r.lease.holder}.`
      : r.state === "switching" ? "Tuning…" : "Not listening - APRS has the radio.";
    $("#ab-freq").textContent = on && r.applied_freq ? mhz(r.applied_freq) : "—";
    $("#ab-light").classList.toggle("on", on && !!t.dcd);
    $("#ab-sql").textContent = on && r.squelch != null ? `${Math.round(r.squelch * 100)}%` : "—";
    for (const el of document.querySelectorAll("#ab-sql-down, #ab-sql-up, #ab-freq-form button, .ab-cell, .ab-ch")) el.disabled = !on;
    $("#ab-play").disabled = !on;
    if (!on && au.want) stopAudio();
    const cur = on ? r.applied_freq : null;
    for (const el of document.querySelectorAll(".ab-cell.cur, .ab-ch.cur")) el.classList.remove("cur");
    if (cur) for (const el of document.querySelectorAll(`[data-freq="${cur}"]`)) el.classList.add("cur");
  }

  const prev = window.onStatus;
  window.onStatus = (st) => {
    if (prev) prev(st);
    if (A.radio && st.radios[A.radio]) render(st.radios[A.radio]);
  };
  load();
})();
