"use strict";
// APRS panel (map, messages, heard, packets) and Direwolf settings.
// Everything received over RF is untrusted: it is only ever rendered with textContent
// or as DOM nodes (never innerHTML), including Leaflet tooltips and popups.
(() => {
  const S = { status: null, stations: new Map(), messages: new Map(), packets: [], bulletins: new Map(), unread: 0, tab: "map" };
  let map = null, layer = null, meMarker = null, fitted = false, centered = false;
  const markers = new Map();
  const MAX_PKTS = 300;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const CARD = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const card = (b) => CARD[Math.round(b / 22.5) % 16];
  const aprsfi = (call) => `https://aprs.fi/#!call=a%2F${encodeURIComponent(call)}`;
  const throttle = (fn, ms) => { let t = 0; return () => { if (!t) t = setTimeout(() => { t = 0; fn(); }, ms); }; };

  // ------------------------------------------------------------ tabs
  function setTab(t) {
    S.tab = t;
    document.querySelectorAll("#aprs-panel .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    document.querySelectorAll("#aprs-panel .tabpane").forEach((p) => (p.hidden = p.dataset.pane !== t));
    if (t === "msgs") { S.unread = 0; renderBadge(); }
    if (t === "map" && map) setTimeout(() => map.invalidateSize(), 0);
    if (t === "pkts") renderPackets();
    if (t === "heard") renderHeard();
  }
  document.querySelectorAll("#aprs-panel .tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

  // ------------------------------------------------------------ status / health
  function renderStatus() {
    const s = S.status;
    if (!s) return;
    $("#aprs-call").textContent = s.callsign + (s.other_calls.length ? ` (also ${s.other_calls.join(", ")})` : "");
    const h = $("#aprs-health");
    h.replaceChildren(
      chip(s.rx ? "RX: listening" : "RX: off", s.rx ? "ok" : "warn", s.rx_reason),
      chip(s.tx ? "TX: on the air" : "TX: not available", s.tx ? "ok" : "warn", s.tx_reason),
      chip(s.link ? "Direwolf link up" : "Direwolf link down", s.link ? "ok" : "bad"),
    );
    $("#aprs-fi-me").href = aprsfi(s.callsign);
    const area = $("#aprs-fi-area");
    area.hidden = !s.position;
    if (s.position) area.href = `https://aprs.fi/#!lat=${s.position[0].toFixed(4)}&lng=${s.position[1].toFixed(4)}&z=11`;
    const btn = $("#msg-form button");
    btn.disabled = !s.tx;
    $("#msg-hint").textContent = s.tx ? "" : `Sending is off: ${s.tx_reason}.`;
    $("#heard-count").textContent = `(${S.stations.size})`;
    updateMe();
  }
  function chip(text, cls, title) {
    const c = el("span", "chip " + (cls || ""), text);
    if (title) c.title = title;
    return c;
  }

  // ------------------------------------------------------------ map
  function ensureMap() {
    if (map) return;
    if (typeof L === "undefined") {
      $("#map-note").textContent = "The map library didn't load (no internet access?). Stations are still listed under Heard.";
      return;
    }
    map = L.map("map", { worldCopyJump: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18, referrerPolicy: "strict-origin-when-cross-origin",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
    }).addTo(map);
    layer = L.layerGroup().addTo(map);
    const p = S.status && S.status.position;
    map.setView(p || [37, -86], p ? 11 : 6);
    centered = !!p;
    $("#map-note").textContent = "Stations heard directly or via digipeaters on RF. Click one for details.";
  }
  function kindColor(s) {
    const code = s.symbol[1];
    if (code === "_") return "#2f7bd9";                        // weather
    if (code === "#" || code === "&") return "#8a4fd6";          // digi / igate
    if (s.speed_kmh || ">kj[<vbsuOR".includes(code)) return "#e07a1f"; // moving
    return "#2f9e62";
  }
  // ------------------------------------------------------------ weather
  // The broker sends weather in fixed units (°C, m/s, mm, hPa); shown in US units like aprs.fi.
  const degF = (c) => c * 9 / 5 + 32, mph = (ms) => ms / 0.44704, inch = (mm) => mm / 25.4;
  function wxLines(w) {
    const out = [], a = [];
    if (w.temperature != null) a.push(`Temperature ${degF(w.temperature).toFixed(0)}°F`);
    if (w.humidity != null) a.push(`Humidity ${Math.round(w.humidity)}%`);
    if (w.pressure != null) a.push(`Pressure ${Math.round(w.pressure)} mbar`);
    if (a.length) out.push(a.join(" · "));
    if (w.wind_speed != null || w.wind_gust != null) {
      let t = "Wind";
      if (w.wind_direction != null) t += ` ${Math.round(w.wind_direction)}° ${card(w.wind_direction)}`;
      if (w.wind_speed != null) t += ` ${mph(w.wind_speed).toFixed(1)} mph`;
      if (w.wind_gust != null) t += ` (gusts ${mph(w.wind_gust).toFixed(1)} mph)`;
      out.push(t);
    }
    const rain = [["rain_1h", "/1h"], ["rain_24h", "/24h"], ["rain_since_midnight", " since midnight"]]
      .filter(([k]) => w[k] != null).map(([k, l]) => `${inch(w[k]).toFixed(2)} in${l}`);
    if (rain.length) out.push("Rain " + rain.join(" · "));
    if (w.luminosity != null) out.push(`Luminosity ${Math.round(w.luminosity)} W/m²`);
    return out;
  }
  function wxShort(w) {
    const p = [];
    if (w.temperature != null) p.push(`${degF(w.temperature).toFixed(0)}°F`);
    if (w.humidity != null) p.push(`${Math.round(w.humidity)}%`);
    if (w.wind_speed != null) p.push(`${w.wind_direction != null ? Math.round(w.wind_direction) + "° " : ""}${mph(w.wind_speed).toFixed(0)} mph`
      + (w.wind_gust != null ? ` g${mph(w.wind_gust).toFixed(0)}` : ""));
    if (w.pressure != null) p.push(`${Math.round(w.pressure)} mbar`);
    return p.join(" · ") || "weather";
  }
  // hover text (plain-text title tooltip): header + the full report
  const wxTitle = (head, w) => [head, ...wxLines(w)].join("\n");

  function tipNode(s) {  // map hover: callsign, plus the weather report for WX stations
    const d = el("div", s.weather ? "wx-tip" : null);
    d.append(el("b", null, s.call));
    if (s.weather) {
      for (const t of wxLines(s.weather)) d.append(el("div", null, t));
      if (s.wx_heard) d.append(el("div", "sub", `Report ${ago(s.wx_heard)}`));
    }
    return d;
  }
  function stationNode(s) {
    const d = el("div");
    const b = el("b", null, s.call);
    d.append(b);
    const lines = [];
    if (s.comment) lines.push(s.comment);
    if (s.status) lines.push(`Status: ${s.status}`);
    if (s.distance_km != null) lines.push(`${s.distance_km.toFixed(1)} km ${card(s.bearing)} (${Math.round(s.bearing)}°)`);
    if (s.speed_kmh && !s.weather) lines.push(`${Math.round(s.speed_kmh)} km/h, course ${s.course ?? "?"}°`);
    if (s.weather) lines.push(...wxLines(s.weather), `Weather report ${ago(s.wx_heard)}`);
    lines.push(`Heard ${ago(s.last_heard)} via ${s.via} · ${s.packets} pkts`);
    for (const t of lines) d.append(el("div", null, t));
    const a = el("a", null, "aprs.fi ↗");
    a.href = aprsfi(s.call); a.target = "_blank"; a.rel = "noopener noreferrer";
    d.append(a);
    return d;
  }
  function updateMarker(s) {
    if (!map || s.lat == null || s.lon == null) return;
    let m = markers.get(s.call);
    const color = kindColor(s);
    if (!m) {
      m = L.circleMarker([s.lat, s.lon], { radius: 7, weight: 2, color, fillColor: color, fillOpacity: 0.65 }).addTo(layer);
      // content is rebuilt on every hover, so a WX station always shows its latest report
      m.bindTooltip(() => tipNode(S.stations.get(s.call) || s), { direction: "top", offset: [0, -6] });
      m.bindPopup(() => stationNode(S.stations.get(s.call) || s));
      markers.set(s.call, m);
    } else {
      m.setLatLng([s.lat, s.lon]);
      m.setStyle({ color, fillColor: color });
    }
  }
  function updateMe() {
    if (!map || !S.status) return;
    const p = S.status.position;
    if (!p) return;
    if (!centered && !fitted) {  // position arrived after the map was drawn (radio GPS)
      map.setView(p, 11);
      centered = true;
    }
    if (!meMarker) {
      meMarker = L.circleMarker(p, { radius: 9, weight: 3, color: "#b86e0a", fillColor: "#e8a33d", fillOpacity: 0.9 }).addTo(map);
      meMarker.bindTooltip(el("span", null, `${S.status.callsign} (this station)`), { permanent: false });
    } else meMarker.setLatLng(p);
  }
  function fitOnce() {
    if (fitted || !map) return;
    const pts = [...S.stations.values()].filter((s) => s.lat != null).map((s) => [s.lat, s.lon]);
    if (S.status && S.status.position) pts.push(S.status.position);
    if (pts.length >= 2) { map.fitBounds(pts, { padding: [30, 30], maxZoom: 12 }); fitted = true; }
  }
  function removeStation(call) {
    S.stations.delete(call);
    const m = markers.get(call);
    if (m) { layer.removeLayer(m); markers.delete(call); }
  }

  // ------------------------------------------------------------ heard
  const renderHeard = throttle(() => {
    $("#heard-count").textContent = `(${S.stations.size})`;
    if (S.tab !== "heard") return;
    const rows = [...S.stations.values()].sort((a, b) => b.last_heard - a.last_heard);
    const frag = document.createDocumentFragment();
    for (const s of rows) {
      const tr = el("tr");
      const td = el("td", "call");
      const a = el("a", null, s.call);
      a.href = aprsfi(s.call); a.target = "_blank"; a.rel = "noopener noreferrer";
      td.append(a);
      tr.append(td,
        el("td", "num", s.distance_km == null ? "—" : `${s.distance_km.toFixed(1)} km`),
        el("td", "num", s.bearing == null ? "—" : `${Math.round(s.bearing)}° ${card(s.bearing)}`),
        el("td", "sub", ago(s.last_heard)),
        el("td", null, s.via),
        el("td", "num", String(s.packets)),
        el("td", "sub", s.weather ? wxShort(s.weather) : (s.status || s.comment || "")));
      if (s.weather) {
        tr.className = "has-wx";
        tr.title = wxTitle(`${s.call} weather (${ago(s.wx_heard)})`, s.weather);
      }
      frag.append(tr);
    }
    $("#heard").replaceChildren(frag);
  }, 500);

  // ------------------------------------------------------------ packets
  const renderPackets = throttle(() => {
    if (S.tab !== "pkts") return;
    const frag = document.createDocumentFragment();
    for (let i = S.packets.length - 1; i >= 0; i--) {
      const p = S.packets[i];
      const tr = el("tr", p.dir === "tx" ? "warning" : "");
      tr.append(el("td", null, fmtTime(p.ts)), el("td", null, p.dir.toUpperCase()), el("td", null, p.src), el("td", null, `${p.fmt}: ${p.summary}`));
      tr.title = p.raw;
      if (p.wx) {
        tr.classList.add("has-wx");
        tr.title = wxTitle(`${p.src} weather`, p.wx) + "\n\n" + p.raw;
      }
      frag.append(tr);
    }
    $("#pkts").replaceChildren(frag);
  }, 300);

  // ------------------------------------------------------------ messages
  function renderBadge() {
    for (const b of [$("#msg-badge"), $("#nav-badge")]) {
      b.hidden = !S.unread;
      b.textContent = String(S.unread);
    }
  }
  window.onViewChange = (v) => {
    if (v !== "aprs") return;
    if (S.tab === "msgs") { S.unread = 0; renderBadge(); }
    if (map) setTimeout(() => { map.invalidateSize(); fitOnce(); }, 0);
  };
  const viewing = () => document.body.dataset.view === "aprs";
  const renderMessages = throttle(() => {
    const list = [...S.messages.values()].sort((a, b) => b.ts - a.ts);
    const frag = document.createDocumentFragment();
    if (!list.length) frag.append(el("li", "sub", "No messages yet."));
    for (const m of list) {
      const li = el("li", (m.direction === "out" ? "out" : "in") + (m.mine ? "" : " other"));
      const who = el("span", "who", m.direction === "out" ? `→ ${m.peer}` : `← ${m.peer}${m.mine ? "" : ` (to ${m.to})`}`);
      who.title = "Reply to this station";
      who.addEventListener("click", () => { const f = $("#msg-form"); f.to.value = m.peer; f.text.focus(); });
      const meta = el("span", "meta");
      if (m.direction === "out") {
        const label = { sending: "sending", retrying: `retrying (${m.tries})`, acked: "✓ delivered", rejected: "✗ rejected", failed: "✗ no ack", cancelled: "cancelled" }[m.state] || m.state;
        meta.append(el("span", `state-${m.state}`, label), document.createTextNode(` · ${fmtTime(m.ts)}`));
        if (m.state === "sending" || m.state === "retrying") {
          const c = el("button", "ghost small", "Cancel");
          c.type = "button";
          c.addEventListener("click", () => api("POST", `/aprs/messages/${m.id}/cancel`, undefined, { quiet: true }));
          meta.append(document.createTextNode(" "), c);
        }
      } else meta.textContent = fmtTime(m.ts);
      li.append(who, el("span", "txt", m.text), meta);
      frag.append(li);
    }
    $("#msgs").replaceChildren(frag);
    const bl = $("#bulletins");
    const bfrag = document.createDocumentFragment();
    for (const b of [...S.bulletins.values()].sort((a, c) => c.ts - a.ts)) bfrag.append(el("li", null, `${b.from} BLN${b.bid}: ${b.text}`));
    if (!S.bulletins.size) bfrag.append(el("li", "sub", "none"));
    bl.replaceChildren(bfrag);
  }, 200);

  const form = $("#msg-form");
  form.text.addEventListener("input", () => ($("#msg-count").textContent = `${form.text.value.length}/67`));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await api("POST", "/aprs/messages", { to: form.to.value.trim().toUpperCase(), text: form.text.value }, { quiet: true });
    if (res.ok) { form.text.value = ""; $("#msg-count").textContent = "0/67"; toast("message queued"); }
    else if (res.data && res.data.reason) toast(res.data.reason, true);
  });

  // ------------------------------------------------------------ data
  function addStation(s) { S.stations.set(s.call, s); updateMarker(s); }
  async function init() {
    let r;
    try { r = await fetch("/aprs", { credentials: "same-origin" }); } catch { return; }
    if (r.status === 404) {
      $("#aprs-panel").hidden = true; $("#nav-aprs").hidden = true; window.aprsKnown = false;
      if (document.body.dataset.view === "aprs") location.hash = "#station";
      return;
    }
    if (!r.ok) return;
    const d = await r.json();
    $("#aprs-panel").hidden = false;
    $("#nav-aprs").hidden = false;
    window.aprsKnown = true;
    S.status = d.status;
    ensureMap();
    S.stations.clear();
    d.stations.forEach(addStation);
    S.messages = new Map(d.messages.map((m) => [m.id, m]));
    S.packets = d.packets;
    S.bulletins = new Map(d.bulletins.map((b) => [`${b.from}:${b.bid}`, b]));
    renderStatus(); renderHeard(); renderMessages(); renderPackets();
    if (viewing()) { setTimeout(() => { map && map.invalidateSize(); fitOnce(); }, 0); }
    initDirewolf();
  }
  window.aprsInit = init;
  window.onStatus = (st) => { if (st.aprs) { S.status = st.aprs; renderStatus(); } };
  window.onAprs = (msg) => {
    const d = msg.data;
    switch (msg.kind) {
      case "station": addStation(d); renderHeard(); fitOnce(); break;
      case "station_gone": removeStation(d.call); renderHeard(); break;
      case "packet": S.packets.push(d); if (S.packets.length > MAX_PKTS) S.packets.splice(0, S.packets.length - MAX_PKTS); renderPackets(); break;
      case "message": {
        const isNew = !S.messages.has(d.id);
        S.messages.set(d.id, d);
        if (isNew && d.direction === "in") {
          if (S.tab !== "msgs" || !viewing()) { S.unread++; renderBadge(); }
          toast(`APRS message from ${d.peer}: ${d.text}`);
        }
        renderMessages();
        break;
      }
      case "bulletin": S.bulletins.set(`${d.from}:${d.bid}`, d); renderMessages(); break;
    }
  };

  // ------------------------------------------------------------ Direwolf settings
  const dw = $("#dw-form");
  async function initDirewolf() {
    let r;
    try { r = await fetch("/direwolf", { credentials: "same-origin" }); } catch { return; }
    if (!r.ok) { $("#dw-panel").hidden = true; return; }
    const d = await r.json();
    $("#dw-panel").hidden = false;
    const running = Object.entries(d.running).map(([rid, svc]) => `${svc} on ${rid}`).join(", ");
    const state = d.mock
      ? (running ? ` · mock mode: simulated ${running} (the real program is not started)` : " · mock mode")
      : (running ? ` · running: ${running}` : " · not running");
    $("#dw-summary").textContent = d.installed
      ? `installed (${d.installed})${state}`
      : "NOT INSTALLED: run `sudo sh deploy/install-direwolf.sh` on the Pi";
    const s = d.settings;
    dw.callsign.value = d.callsign;
    for (const k of ["adevice", "rigctld_port", "txdelay", "txtail", "beacon_every_min", "symbol", "comment", "digipeater", "igate_server"]) dw[k].value = s[k];
    dw.arate.value = String(s.arate);
    dw.lat.value = s.lat ?? ""; dw.lon.value = s.lon ?? "";
    dw.beacon.checked = s.beacon; dw.igate.checked = s.igate; dw.igate_tx.checked = s.igate_tx;
    dw.beacon_path.value = s.beacon_path.join(",");
    dw.passcode.value = "";
    dw.passcode.placeholder = s.passcode === "set" ? "•••• (set; type to change)" : "auto from callsign";
  }
  function collect() {
    const num = (v) => (v === "" ? null : Number(v));
    const out = {
      adevice: dw.adevice.value.trim(), arate: Number(dw.arate.value), rigctld_port: Number(dw.rigctld_port.value),
      txdelay: Number(dw.txdelay.value), txtail: Number(dw.txtail.value),
      beacon: dw.beacon.checked, beacon_every_min: Number(dw.beacon_every_min.value),
      lat: num(dw.lat.value), lon: num(dw.lon.value), symbol: dw.symbol.value, comment: dw.comment.value,
      beacon_path: dw.beacon_path.value ? dw.beacon_path.value.split(",") : [],
      digipeater: dw.digipeater.value, igate: dw.igate.checked, igate_tx: dw.igate_tx.checked,
      igate_server: dw.igate_server.value.trim(),
    };
    if (dw.passcode.value.trim() !== "") out.passcode = Number(dw.passcode.value.trim());
    return out;
  }
  async function save() {
    const r = await api("PUT", "/direwolf/settings", collect(), { quiet: true });
    if (r.ok) { toast("Direwolf settings saved"); initDirewolf(); }
    return r.ok;
  }
  dw.addEventListener("submit", (e) => { e.preventDefault(); save(); });
  $("#dw-apply").addEventListener("click", async () => {
    if (!(await save())) return;
    const r = await api("POST", "/direwolf/apply", undefined, { quiet: true });
    if (r.ok) toast([...r.data.restarted, ...r.data.notes].join("; ") || "applied");
    initDirewolf();
  });
  $("#dw-preview").addEventListener("click", async () => {
    const r = await fetch(`/direwolf/preview/${encodeURIComponent($("#dw-prev-inst").value)}`, { credentials: "same-origin" });
    const pre = $("#dw-preview-text");
    pre.textContent = await r.text();
    pre.hidden = false;
  });
})();
