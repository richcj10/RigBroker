"use strict";
// Packet BBS terminal (Packet tab). Text from RF is untrusted: it is only ever put into the
// page with textContent, never innerHTML.
(() => {
  const B = { stations: [], state: "idle", session: null, last: null, mycall: "", history: [], hpos: 0 };
  const MAX_CHUNKS = 3000;
  const term = $("#bbs-term");
  const STATE_CHIP = {
    idle: ["idle", ""], tuning: ["tuning radio…", "warn"], connecting: ["connecting…", "warn"],
    connected: ["connected", "ok"], disconnecting: ["disconnecting…", "warn"],
  };

  // ------------------------------------------------------------ terminal
  function addChunk(ch) {
    const nearBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
    const span = document.createElement("span");
    span.className = `t-${ch.dir}`;
    span.textContent = ch.text;
    term.append(span);
    while (term.childNodes.length > MAX_CHUNKS) term.firstChild.remove();
    if (nearBottom) term.scrollTop = term.scrollHeight;
  }

  // ------------------------------------------------------------ station + path pickers
  function fillStations() {
    const sel = $("#bbs-call");
    const keep = sel.value;
    sel.replaceChildren(...B.stations.map((s) => {
      const o = document.createElement("option");
      o.value = s.call;
      o.textContent = `${s.call} · ${(s.freq / 1e6).toFixed(3)} MHz${s.name ? " · " + s.name : ""}`;
      return o;
    }));
    if (B.stations.some((s) => s.call === keep)) sel.value = keep;
    fillPaths();
  }
  function fillPaths() {
    const st = B.stations.find((s) => s.call === $("#bbs-call").value);
    const sel = $("#bbs-path");
    const keep = sel.value;
    const opts = [["", "Direct"], ...(st ? st.via : []).map((d) => [d, `via ${d}`]), ["*", "Custom path…"]];
    sel.replaceChildren(...opts.map(([v, label]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = label;
      return o;
    }));
    if (opts.some(([v]) => v === keep)) sel.value = keep;
    $("#bbs-custom-wrap").hidden = sel.value !== "*";
  }
  $("#bbs-call").addEventListener("change", fillPaths);
  $("#bbs-path").addEventListener("change", () => {
    $("#bbs-custom-wrap").hidden = $("#bbs-path").value !== "*";
  });

  // ------------------------------------------------------------ state
  function renderState() {
    const [text, cls] = STATE_CHIP[B.state] || [B.state, ""];
    const chip = $("#bbs-state");
    chip.textContent = text;
    chip.className = "chip" + (cls ? " " + cls : "");
    const s = B.session;
    const idle = B.state === "idle";
    $("#bbs-connect").hidden = !idle;
    $("#bbs-disconnect").hidden = idle;
    $("#bbs-call").disabled = $("#bbs-path").disabled = !idle;
    const connected = B.state === "connected";
    $("#bbs-line").disabled = $("#bbs-send").disabled = !connected;
    $("#bbs-line").placeholder = connected ? "Type a command and press Enter (e.g. L, R 12, B)"
      : "Connect first - then type a command and press Enter (e.g. L, R 12, B)";
    let sub = `You connect as ${B.mycall || "?"}. `;
    if (s) sub += `${s.call}${s.via.length ? " via " + s.via.join(",") : " direct"} for ${s.holder} on ${(s.freq / 1e6).toFixed(3)} MHz.`;
    else if (B.last) sub += `Last session: ${B.last.call} - ${B.last.reason}.`;
    $("#bbs-sub").textContent = sub;
    if (connected && document.activeElement !== $("#bbs-line") && document.body.dataset.view === "packet") {
      $("#bbs-line").focus();
    }
  }
  function applyStatus(st) {
    B.state = st.state; B.session = st.session; B.last = st.last; B.mycall = st.mycall;
    renderState();
  }

  async function init() {
    let r;
    try { r = await fetch("/bbs"); } catch { return; }
    if (r.status === 404) { $("#bbs-panel").hidden = true; return; }  // no bbs: section
    if (!r.ok) return;
    const d = await r.json();
    $("#bbs-panel").hidden = false;
    B.stations = d.stations;
    fillStations();
    term.replaceChildren();
    for (const ch of d.scrollback) addChunk(ch);
    term.scrollTop = term.scrollHeight;
    applyStatus(d);
  }
  window.bbsInit = init;
  window.onBbs = (msg) => {
    if (msg.kind === "text") addChunk(msg.data);
    else if (msg.kind === "status") applyStatus(msg.data);
  };

  // ------------------------------------------------------------ actions
  $("#bbs-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const path = $("#bbs-path").value;
    const via = path === "*"
      ? e.target.custom.value.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean)
      : path ? [path] : [];
    await api("POST", "/bbs/connect", { call: $("#bbs-call").value, via });
  });
  $("#bbs-disconnect").addEventListener("click", () => api("POST", "/bbs/disconnect", {}));
  $("#bbs-input").addEventListener("submit", async (e) => {
    e.preventDefault();
    const line = $("#bbs-line").value;
    const res = await api("POST", "/bbs/send", { text: line });
    if (res.ok) {
      if (line && B.history[B.history.length - 1] !== line) B.history.push(line);
      B.history = B.history.slice(-100);
      B.hpos = B.history.length;
      $("#bbs-line").value = "";
    }
  });
  $("#bbs-line").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    B.hpos = Math.max(0, Math.min(B.history.length, B.hpos + (e.key === "ArrowUp" ? -1 : 1)));
    $("#bbs-line").value = B.history[B.hpos] || "";
  });
  $("#bbs-add-toggle").addEventListener("click", () => {
    const f = $("#bbs-add");
    f.hidden = !f.hidden;
    if (!f.hidden) f.call.focus();
  });
  $("#bbs-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const res = await api("POST", "/bbs/stations", {
      call: f.call.value.trim(), freq: Math.round(parseFloat(f.mhz.value) * 1e6), name: f.name.value.trim(),
      via: f.via.value.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean),
    });
    if (res.ok) { f.reset(); f.hidden = true; init(); }
  });

  const prevView = window.onViewChange;
  window.onViewChange = (v) => {
    if (prevView) prevView(v);
    if (v === "packet") term.scrollTop = term.scrollHeight;
  };
  init();
})();
