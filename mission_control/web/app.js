/* Zee-1 mission-control dashboard.
 * Polls the FastAPI backend (mission_control/api.py) and renders live state
 * into the HTML panels. Telemetry values shown are simulated data produced by
 * the deterministic spacecraft models -- not real satellite data.
 */
"use strict";

const API_TOKEN = window.SIM_API_TOKEN || "";
const API_BASE = "";
const GS = { name: "KUCHING-GS", lat: 1.5533, lon: 110.3592 };

const els = (id) => document.getElementById(id);
const f1 = (x) => (x == null ? "—" : Number(x).toFixed(1));
const f2 = (x) => (x == null ? "—" : Number(x).toFixed(2));

async function apiGet(path, tries = 0) {
  const res = await fetch(API_BASE + path, {
    headers: { "X-API-Token": API_TOKEN, "Content-Type": "application/json" },
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw Object.assign(new Error(detail.detail || res.statusText), { status: res.status });
  }
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(API_BASE + path, {
    method: "POST",
    headers: { "X-API-Token": API_TOKEN, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

async function apiDelete(path) {
  const res = await fetch(API_BASE + path, {
    method: "DELETE",
    headers: { "X-API-Token": API_TOKEN },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

// ----------------------------------------------------------------- charts
const chartDefs = [
  { el: "ch-temp", field: "temperature_c", y: "°C" },
  { el: "ch-batt", field: "battery_percentage", y: "%" },
  { el: "ch-solar", field: "solar_power", y: "W" },
  { el: "ch-cpu", field: "cpu_usage", y: "%" },
  { el: "ch-mem", field: "memory_usage", y: "%" },
  { el: "ch-alt", field: "altitude_km", y: "km" },
];

const charts = {};
for (const d of chartDefs) {
  const ctx = els(d.el);
  charts[d.field] = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [{ label: d.field, data: [], borderColor: "#35d0ba", backgroundColor: "rgba(53,208,186,0.12)", fill: true, pointRadius: 0, borderWidth: 2, tension: 0.25 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#7c8ca0", maxTicksLimit: 6 }, grid: { color: "#16202b" } },
        y: { ticks: { color: "#7c8ca0", maxTicksLimit: 4 }, grid: { color: "#16202b" }, title: { display: true, color: "#7c8ca0", text: d.y } },
      },
    },
  });
}

// ------------------------------------------------------------------ map
function drawMap(points, sat) {
  const c = els("map-canvas");
  const ctx = c.getContext("2d");
  const W = c.width, H = c.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#0b1017";
  ctx.fillRect(0, 0, W, H);

  const px = (lon) => ((lon + 180) / 360) * W;
  const py = (lat) => ((90 - lat) / 180) * H;

  // graticule
  ctx.strokeStyle = "#162735";
  ctx.lineWidth = 1;
  for (let lon = -180; lon <= 180; lon += 30) {
    ctx.beginPath();
    ctx.moveTo(px(lon), 0); ctx.lineTo(px(lon), H); ctx.stroke();
  }
  for (let lat = -90; lat <= 90; lat += 30) {
    ctx.beginPath();
    ctx.moveTo(0, py(lat)); ctx.lineTo(W, py(lat)); ctx.stroke();
  }
  ctx.strokeStyle = "#43586e";
  ctx.strokeRect(0, 0, W, H);

  // ground track (lat/lon pairs)
  if (points.length > 1) {
    ctx.beginPath();
    ctx.strokeStyle = "rgba(53,208,186,0.75)";
    ctx.lineWidth = 2;
    points.forEach((p, i) => {
      const x = px(p.longitude), y = py(p.latitude);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  // ground station
  ctx.fillStyle = "#e8c547";
  ctx.beginPath(); ctx.arc(px(GS.lon), py(GS.lat), 4, 0, 6.28); ctx.fill();
  ctx.font = "11px sans-serif";
  ctx.fillText(GS.name, px(GS.lon) + 7, py(GS.lat) + 4);

  // sub-satellite point
  if (sat && sat.longitude != null) {
    const x = px(sat.longitude), y = py(sat.latitude);
    ctx.strokeStyle = "#e5605a";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(x - 9, y); ctx.lineTo(x + 9, y); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x, y - 9); ctx.lineTo(x, y + 9); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#e5605a";
    ctx.beginPath(); ctx.arc(x, y, 4, 0, 6.28); ctx.fill();
  }
}

// -------------------------------------------------------------- rendering
const MODE_CLASS = {
  BOOT: "info", INIT: "info", NORMAL: "ok", SAFE: "warn",
  PAYLOAD_OPERATION: "ok", COMMUNICATION: "ok",
};

function setBadge(el, text, cls) {
  el.textContent = text;
  el.className = "badge" + (cls ? " " + cls : "");
}

function renderStatus(s) {
  const sat = s.satellite;
  setBadge(els("conn-dot").classList.contains("on") ? els("conn-dot") : els("conn-dot"), "", "");
  els("sat-id").textContent = sat.satellite_id;
  setBadge(els("sat-mode"), sat.operating_mode, MODE_CLASS[sat.operating_mode] || "info");
  setBadge(els("sat-comm"), sat.communication_status,
    sat.communication_status === "TRANSMITTING" ? "ok" : "info");
  setBadge(els("sat-payload"), sat.payload_status,
    sat.payload_status === "ACTIVE" ? "ok" : "info");
  els("sat-mtime").textContent = f1(sat.mission_time_s) + " s";
  setBadge(els("sat-faults"),
    sat.active_faults && sat.active_faults.length ? sat.active_faults.join(", ") : "none",
    sat.active_faults && sat.active_faults.length ? "warn" : "");

  els("sat-comm").textContent = sat.communication_status;
  els("sat-payload").textContent = sat.payload_status;

  // power
  const batt = sat.battery_percentage || 0;
  els("batt-pct").textContent = f1(batt) + " %";
  els("batt-bar").style.width = batt + "%";
  els("batt-bar").className = "fill" + (batt < 15 ? " crit" : batt < 40 ? " warn" : "");
  els("batt-v").textContent = f2(sat.battery_voltage) + " V";
  els("solar-w").textContent = f1(sat.solar_power) + " W";
  els("cons-w").textContent = f1(sat.power_consumption) + " W";
  els("net-w").textContent = f1((sat.solar_power || 0) - (sat.power_consumption || 0)) + " W";

  // thermal
  const temp = sat.temperature_c || 0;
  els("temp-val").textContent = f1(temp);
  els("temp-val").className = "big-val" + (temp >= 70 ? " error" : temp >= 50 ? " warn" : "");
  setBadge(els("temp-status"), temp >= 70 ? "CRITICAL" : temp >= 50 ? "WARNING" : "NOMINAL",
    temp >= 70 ? "crit" : temp >= 50 ? "warn" : "ok");

  // computing
  els("cpu-pct").textContent = f1(sat.cpu_usage) + " %";
  els("cpu-bar").style.width = (sat.cpu_usage || 0) + "%";
  els("mem-pct").textContent = f1(sat.memory_usage) + " %";
  els("mem-bar").style.width = (sat.memory_usage || 0) + "%";

  // position
  els("pos-lat").textContent = f2(sat.latitude) + "°";
  els("pos-lon").textContent = f2(sat.longitude) + "°";
  els("pos-alt").textContent = f1(sat.altitude_km) + " km";
  els("pos-vel").textContent = f2(sat.velocity_kms) + " km/s";
  els("pos-el").textContent = f1(sat.elevation_deg) + "°";
  setBadge(els("pos-vis"), sat.visibility,
    sat.visibility === "VISIBLE" ? "ok" : "");

  // updated orbit + attitude readouts
  els("pos-orb").textContent =
    (sat.orbit_altitude_km == null ? "—" : f1(sat.orbit_altitude_km) + " km / " +
     f1(sat.orbit_period_s) + " s");
  setBadge(els("pos-att"), sat.attitude_target || "—",
    sat.attitude_target === "SUN_POINTING" ? "ok" : "info");

  // link / comm
  const link = s.link || {};
  const gs = s.ground_station || {};
  els("comm-rx").textContent = gs.packets_received;
  els("comm-ok").textContent = gs.packets_accepted;
  els("comm-loss").textContent = link.packets_lost;
  els("comm-crc").textContent = gs.packets_corrupted + link.packets_corrupted;
  els("comm-miss").textContent = gs.packets_missing;
  els("comm-rej").textContent = gs.packets_rejected;
  els("comm-lat").textContent = f1(link.avg_latency_ms) + " ms";
  els("comm-rate").textContent = link.data_rate_bps + " bps";
  els("comm-ebn0").textContent = link.eb_n0_db == null ? "—" : f1(link.eb_n0_db) + " dB";
  els("comm-ber").textContent =
    link.measured_ber == null ? "—" : Number(link.measured_ber).toExponential(2);
  els("comm-mod").textContent = link.modulation || "—";
  const q = link.link_quality_percent || 0;
  setBadge(els("comm-quality"), f1(q) + "%", q >= 85 ? "ok" : q >= 50 ? "warn" : "err");
  const ebn0Input = els("ebn0-input");
  if (ebn0Input && link.eb_n0_db != null &&
      document.activeElement !== ebn0Input) {
    ebn0Input.value = link.eb_n0_db;
  }

  // onboard storage
  const stor = sat.onboard_storage || {};
  els("queue-pct").textContent = f1(stor.usage_percent) + " %";
  els("queue-bar").style.width = (stor.usage_percent || 0) + "%";
  els("queue-n").textContent = stor.used_packets;
  els("queue-cap").textContent = stor.capacity;
  els("queue-drop").textContent = stor.dropped;
}

function renderRun(running) {
  els("sim-state").textContent = running ? "simulation running" : "simulation stopped";
  els("sim-state").classList = "pill";
  if (!running) setBadge(els("conn-dot"), "", "off");
}

function renderFeeds(events, security) {
  renderFeed("events-feed", events);
  renderFeed("security-feed", security);
}

function renderFeed(id, items) {
  const feed = els(id);
  const rows = (items || []).slice(0, 60).map((e) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML =
      '<span class="when"></span>' +
      '<span class="sev ' + (e.severity || "INFO") + '">' + (e.severity || "") + "</span>" +
      '<span class="src">' + (e.source || "") + "</span>" +
      '<span class="msg"></span>';
    // Real wall-clock time of the event (never corrupted by link noise).
    const t = e.timestamp ? new Date(e.timestamp * 1000) : null;
    row.querySelector(".when").textContent =
      t ? t.toLocaleTimeString() + "." + String(t.getMilliseconds()).padStart(3, "0") : "—";
    row.querySelector(".msg").textContent = e.event_type + " — " + e.message;
    return row;
  });
  if (feed.__timer) clearTimeout(feed.__timer);
  feed.replaceChildren(...rows);
  feed.scrollTop = feed.scrollHeight;
}

// ------------------------------------------------------------------ polls
async function pollStatus() {
  try {
    const s = await apiGet("/api/status");
    const dot = els("conn-dot");
    dot.classList.add("on"); dot.classList.remove("off", "vis");
    renderStatus(s);
    renderRun(s.running);
    if (s.satellite && s.satellite.visibility === "VISIBLE") dot.classList.add("vis");
  } catch (e) {
    els("conn-dot").classList.remove("on", "vis");
    els("conn-dot").classList.add("off");
  }
}

async function pollSeries() {
  const field = chartDefs[Math.floor(Math.random() * chartDefs.length)].field;
  for (const d of chartDefs) {
    try {
      const rows = await apiGet("/api/telemetry/series?field=" + d.field + "&limit=300");
      const ch = charts[d.field];
      ch.data.labels = rows.map((r) => f1(r.mission_time));
      ch.data.datasets[0].data = rows.map((r) => r.value);
      ch.update();
    } catch (e) { /* ignore transient */ }
  }
}

async function pollTelemetry() {
  try {
    const rows = await apiGet("/api/telemetry/history?limit=200");
    const pts = rows.map((r) => ({
      latitude: Number(r.latitude), longitude: Number(r.longitude),
    }));
    const s = await apiGet("/api/status");
    drawMap(pts, s.satellite);
  } catch (e) { /* ignore transient */ }
}

async function pollEvents() {
  Promise.all([apiGet("/api/events?limit=60"), apiGet("/api/security/events?limit=30")])
    .then(([events, security]) => renderFeeds(events, security))
    .catch(() => {});
}

// ------------------------------------------------------------------ ops
async function loadFaults() {
  try {
    const data = await apiGet("/api/simulation/faults");
    const sel = els("fault-select");
    sel.replaceChildren(...data.available.map((f) => {
      const o = document.createElement("option");
      o.value = f.name;
      o.textContent = f.name + " (" + f.severity + ")";
      return o;
    }));
  } catch (e) {}
}

function setResult(id, text, ok) {
  const el = els(id);
  el.textContent = text;
  el.style.color = ok === false ? "var(--err)" : ok ? "var(--ok)" : "";
}

els("btn-start").addEventListener("click", () => {
  apiPost("/api/simulation/start", {}).then(() => setResult("cmd-result", "simulation started", true)).catch((e) => setResult("cmd-result", String(e.message), false));
});
els("btn-stop").addEventListener("click", () => {
  apiPost("/api/simulation/stop", {}).then(() => setResult("cmd-result", "simulation stopped", true)).catch((e) => setResult("cmd-result", String(e.message), false));
});
els("btn-reset").addEventListener("click", () => {
  apiPost("/api/simulation/reset", {}).then(() => setResult("cmd-result", "simulation reset + started", true)).catch((e) => setResult("cmd-result", String(e.message), false));
});

els("btn-fault").addEventListener("click", () => {
  const fault = els("fault-select").value;
  apiPost("/api/simulation/fault", { fault }).then(() => setResult("fault-result", fault + " injected", true)).catch((e) => setResult("fault-result", String(e.message), false));
});
els("btn-clear-fault").addEventListener("click", () => {
  const fault = els("fault-select").value;
  apiDelete("/api/simulation/fault/" + encodeURIComponent(fault)).then(() => setResult("fault-result", fault + " cleared", true)).catch((e) => setResult("fault-result", String(e.message), false));
});

els("btn-ebn0").addEventListener("click", () => {
  const db = parseFloat(els("ebn0-input").value);
  if (Number.isNaN(db)) { setResult("ebn0-result", "invalid", false); return; }
  apiPost("/api/link/eb-n0", { eb_n0_db: db }).then((s) => setResult("ebn0-result", "applied: quality " + s.link_quality_percent + "%", true)).catch((e) => setResult("ebn0-result", String(e.message), false));
});

function syncParamWidgets() {
  const cmd = els("cmd-select").value;
  els("cmd-param-label").hidden = cmd !== "SET_MODE";
  els("cmd-mode").hidden = cmd !== "SET_MODE";
  els("cmd-dv").hidden = cmd !== "ORBITAL_BURN";
  els("cmd-dv-unit").hidden = cmd !== "ORBITAL_BURN";
  els("cmd-att").hidden = cmd !== "ADJUST_ATTITUDE";
  els("cmd-param-label").textContent =
    cmd === "ORBITAL_BURN" ? "delta-v" :
    cmd === "ADJUST_ATTITUDE" ? "attitude" : "mode";
  return cmd;
}

els("cmd-select").addEventListener("change", syncParamWidgets);
syncParamWidgets();

els("btn-cmd").addEventListener("click", async () => {
  const command = els("cmd-select").value;
  let parameters = {};
  if (command === "SET_MODE") parameters = { mode: els("cmd-mode").value };
  else if (command === "ORBITAL_BURN") {
    const dv = parseFloat(els("cmd-dv").value);
    if (Number.isNaN(dv)) { setResult("cmd-result", "invalid delta-v", false); return; }
    parameters = { delta_v_mps: dv };
  } else if (command === "ADJUST_ATTITUDE") {
    parameters = { attitude: els("cmd-att").value };
  }
  try {
    const sent = await apiPost("/api/command/send", { command, parameters });
    setResult("cmd-result", "uplinked seq " + sent.sequence + " …", true);
    const seq = sent.sequence;
    for (let i = 0; i < 10; i++) {
      await new Promise((r) => setTimeout(r, 700));
      const res = await apiGet("/api/command/result/" + seq);
      if (!res.pending) {
        const r = res.result || {};
        setResult("cmd-result",
          "seq " + seq + " → " + (r.status || r.result || "executed"), true);
        return;
      }
    }
    setResult("cmd-result", "seq " + seq + " accepted, no uplink visibility yet", false);
  } catch (e) {
    setResult("cmd-result", String(e.message), false);
  }
});

// ------------------------------------------------------------------ chat
let chatSignature = "";

function chatRow(msg) {
  const row = document.createElement("div");
  row.className = "row chat-row";
  const t = msg.sent_at ? new Date(msg.sent_at * 1000) : null;
  const when = t ? t.toLocaleTimeString() : "—";
  row.innerHTML =
    '<span class="when">' + when + "</span>" +
    '<span class="src">' + (msg.label || msg.direction || "") + "</span>" +
    '<span class="msg"><b>#' + msg.message_id +
      "</b> " + (msg.delivered || 0) + " ok / " + (msg.corrupted || 0) + " corr / " +
      (msg.lost || 0) + " lost</span>" +
    '<div class="chat-recv">received: ' +
    (typeof msg.received_text === "string" && msg.received_text.length
      ? msg.received_text : "(nothing arrived)") + "</div>";
  return row;
}

async function pollChat() {
  try {
    const data = await apiGet("/api/chat/history?limit=30");
    const sig = data.map((m) =>
      m.message_id + ":" + m.delivered + "/" + m.corrupted + "/" + m.lost +
      ":" + (m.complete ? "1" : "0") + ":" + (m.received_text || "")).join("|");
    if (sig === chatSignature) return;
    chatSignature = sig;
    const log = els("chat-log");
    log.replaceChildren();
    for (const m of data) {
      if (m.direction) log.appendChild(chatRow(m));
    }
    log.scrollTop = log.scrollHeight;
  } catch (e) { /* ignore transient */ }
}

els("btn-chat").addEventListener("click", async () => {
  const text = els("chat-input").value.trim();
  const direction = els("chat-direction").value;
  if (!text) { setResult("chat-result", "type a message first", false); return; }
  try {
    const msg = await apiPost("/api/chat/send", { direction, text });
    els("chat-input").value = "";
    setResult("chat-result",
      "sent #" + msg.message_id + " (" + msg.chunk_count + " chunk(s)) — watch below", true);
    pollChat();
  } catch (e) {
    setResult("chat-result", String(e.message), false);
  }
});

// ------------------------------------------------------------- scheduler
loadFaults();
pollStatus();
pollTelemetry();
pollEvents();
pollSeries();
pollChat();

setInterval(pollStatus, 1000);
setInterval(pollTelemetry, 2500);
setInterval(pollEvents, 2000);
setInterval(pollSeries, 3000);
setInterval(pollChat, 1500);
setInterval(loadFaults, 15000);