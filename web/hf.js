"use strict";
// HF tab (FT-891): manual session, frequency / band / mode, Tune, TX meters.
// The broker enforces band edges, modes and the power cap; this page only asks.
(() => {
  // default spot per band when the band hasn't been used yet (band memory wins)
  const BANDS = [
    ["160m", 1900000, "LSB"], ["80m", 3850000, "LSB"], ["60m", 5357000, "USB"],
    ["40m", 7200000, "LSB"], ["30m", 10136000, "PKTUSB"], ["20m", 14200000, "USB"],
    ["17m", 18130000, "USB"], ["15m", 21300000, "USB"], ["12m", 24950000, "USB"],
    ["10m", 28400000, "USB"], ["6m", 50125000, "USB"],
  ];
  // band edges (Hz) used only to highlight the current band; the broker enforces the real ones
  const EDGES = {
    "160m": [1800000, 2000000], "80m": [3500000, 4000000], "60m": [5330500, 5406000],
    "40m": [7000000, 7300000], "30m": [10100000, 10150000], "20m": [14000000, 14350000],
    "17m": [18068000, 18168000], "15m": [21000000, 21450000], "12m": [24890000, 24990000],
    "10m": [28000000, 29700000], "6m": [50000000, 54000000],
  };
  const STEPS = [[-10000, "−10k"], [-1000, "−1k"], [-100, "−100"], [100, "+100"], [1000, "+1k"], [10000, "+10k"]];
  const H = { id: null, r: null, manual: null };

  function hfRadio(st) {
    for (const [id, r] of Object.entries(st.radios || {})) {
      if (r.tab !== "hf") continue;
      const manual = Object.entries(r.profiles).find(([, p]) => p.manual);
      if (manual) return [id, r, manual[0]];
    }
    return [null, null, null];
  }
  const inRanges = (prof, hz) => prof.freq_ranges.some(([lo, hi]) => hz >= lo && hz <= hi);

  async function tune(freq, mode) {
    const body = {};
    if (freq != null) body.freq = Math.round(freq);
    if (mode) body.mode = mode;
    const res = await api("POST", `/radios/${H.id}/tune`, body, { quiet: true });
    if (!res.ok && res.data && res.data.reason) toast(res.data.reason, true);
  }

  // ------------------------------------------------------------ static controls
  $("#hf-steps").replaceChildren(...STEPS.map(([d, label]) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "ghost small"; b.textContent = label;
    b.addEventListener("click", () => H.r && H.r.applied_freq && tune(H.r.applied_freq + d));
    return b;
  }));
  $("#hf-bands").replaceChildren(...BANDS.map(([band, f, m]) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "small"; b.textContent = band.replace("m", ""); b.dataset.band = band;
    b.title = `${band}: last used frequency, or ${(f / 1e6).toFixed(3)} MHz ${m}`;
    b.addEventListener("click", () => {
      const mem = H.r && H.r.band_memory && H.r.band_memory[band];
      tune(mem ? mem.freq : f, mem ? mem.mode : m);
    });
    return b;
  }));
  $("#hf-freq-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const mhz = parseFloat($("#hf-mhz").value);
    if (Number.isFinite(mhz)) tune(mhz * 1e6);
  });
  $("#hf-mode").addEventListener("change", () => tune(null, $("#hf-mode").value));
  $("#hf-tune").addEventListener("click", async () => {
    $("#hf-tune").disabled = true;
    const res = await api("POST", `/radios/${H.id}/atu`, {}, { quiet: true });
    if (!res.ok && res.data && res.data.reason) toast(res.data.reason, true);
    $("#hf-tune").disabled = false;
  });
  $("#hf-start").addEventListener("click", () => {
    if (H.r && H.r.lease) api("POST", `/radios/${H.id}/release`, { holder: "web" });
    else api("POST", `/radios/${H.id}/lease`, { profile: H.manual, holder: "web" });
  });

  // ------------------------------------------------------------ status
  function meter(key, value, max, text) {
    const fill = $(`#hf-meters [data-m="${key}"]`);
    fill.style.width = value == null ? "0%" : `${Math.min(100, (value / max) * 100)}%`;
    $(`#hf-meters [data-v="${key}"]`).textContent = value == null ? "—" : text;
  }
  function render(st) {
    const [id, r, manual] = hfRadio(st);
    $("#nav-hf").hidden = !id;
    $("#hf-panel").hidden = !id;
    if (!id) return;
    H.id = id; H.r = r; H.manual = manual;
    const prof = r.profiles[manual];
    const inSession = r.applied_profile === manual && r.state === "active";
    const t = r.telemetry;
    $("#hf-title").textContent = `${r.name} · HF`;
    $("#hf-start").textContent = r.lease ? "End session (radio off)" : "Start HF session";
    $("#hf-start").className = r.lease ? "danger" : "";
    $("#hf-start").disabled = r.state === "switching" || r.state === "returning" || !!r.fault;
    $("#hf-sub").textContent = r.fault ? `FAULT: ${r.fault.reason}`
      : inSession ? `In session (${r.lease ? r.lease.holder : "?"}) · ${r.applied_mode || ""}`
      : r.state === "switching" ? "Starting…" : "Radio off - start a session to operate.";
    $("#hf-controls").classList.toggle("disabled", !inSession);
    for (const el of $("#hf-controls").querySelectorAll("button, input, select")) el.disabled = !inSession || !!t.ptt;
    // frequency box: follow the radio unless the user is typing in it
    if (document.activeElement !== $("#hf-mhz") && r.applied_freq) $("#hf-mhz").value = (r.applied_freq / 1e6).toFixed(4);
    const modeSel = $("#hf-mode");
    if (modeSel.dataset.key !== prof.modes.join()) {
      modeSel.dataset.key = prof.modes.join();
      modeSel.replaceChildren(...prof.modes.map((m) => { const o = document.createElement("option"); o.value = o.textContent = m; return o; }));
    }
    if (r.applied_mode && document.activeElement !== modeSel) modeSel.value = r.applied_mode;
    $("#hf-tune").hidden = !r.tune;
    if (r.tune) $("#hf-tune").title = `Short carrier for the auto-tuner: ${r.tune.mode}, ${Math.round(r.tune.power * 100)} W, ${r.tune.seconds} s`;
    // band buttons: current band highlighted; bands outside the allowed edges disabled
    for (const b of $("#hf-bands").querySelectorAll("button")) {
      const [, f] = BANDS.find(([band]) => band === b.dataset.band);
      const mem = r.band_memory && r.band_memory[b.dataset.band];
      b.disabled = b.disabled || !inRanges(prof, mem ? mem.freq : f);
      const [lo, hi] = EDGES[b.dataset.band];
      b.classList.toggle("on", !!r.applied_freq && r.applied_freq >= lo && r.applied_freq <= hi);
    }
    // TX meters (only while transmitting)
    const tx = !!t.ptt;
    $("#hf-meters").classList.toggle("live", tx);
    meter("swr", tx ? t.swr : null, 5, t.swr != null ? `${t.swr.toFixed(1)}:1` : "—");
    meter("alc", tx ? t.alc : null, 1, t.alc != null ? `${Math.round(t.alc * 100)}%` : "—");
    meter("po", tx ? t.po_w : null, 100, t.po_w != null ? `${Math.round(t.po_w)} W` : "—");
  }

  const prev = window.onStatus;
  window.onStatus = (st) => { if (prev) prev(st); render(st); };
})();
