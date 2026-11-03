// The Analysis tab (protocol 6). The Pi analyzes a drive (carpi_stats.py, run by carpi_drives.py in
// the Bluetooth service): the one being recorded, followed live, or a saved one. The page shows a
// summary, an ECU-style map of any value over any two others, and events.
// Loaded after config.js and uses its helpers: $, esc, store, num, app, chip, toast, connected,
// fmtDuration, ago, showTemp, tempUnit, DemoLink.

const AN_TICK_MS = 1000;             // one request per tick at most
const AN_LIVE_EVERY_MS = 3000;       // the drive being recorded: analysis and map each this often
const AN_PRESETS = {
  trim: { label: "Fuel trim", x: "rpm", y: "load_pct", z: "trim_total", filters: ["running", "warm", "closed_loop"], stat: "mean" },
  timing: { label: "Timing", x: "rpm", y: "load_pct", z: "timing_deg", filters: ["running", "no_shift"], stat: "mean" },
  lambda: { label: "Lambda error", x: "rpm", y: "load_pct", z: "lambda_err", filters: ["running", "no_shift"], stat: "mean" },
  boost: { label: "Boost", x: "rpm", y: "pedal_pct", z: "boost_psi", filters: ["running", "no_shift"], stat: "mean" },
};
const AN_FILTERS = [["running", "Running"], ["warm", "Warm"], ["closed_loop", "Closed loop"], ["idle", "Idle"],
  ["moving", "Moving"], ["cruise", "Cruise"], ["boost", "Under boost"], ["hard", "Hard throttle"], ["no_shift", "No gear shifts"]];
const AN_STATS = { mean: "Average", min: "Lowest", max: "Highest", rows: "Rows" };
// column -> [label, unit, map step]: carpi_stats.COLUMNS (tests/test_stats.py keeps them equal)
const AN_COLS = {
  rpm: ["Engine speed", "rpm", 500],
  speed_kph: ["Vehicle speed", "km/h", 10],
  pedal_pct: ["Accelerator pedal", "%", 10],
  throttle_pct: ["Throttle plate", "%", 10],
  load_pct: ["Calculated load", "%", 10],
  abs_load_pct: ["Absolute load", "%", 10],
  map_kpa: ["Manifold pressure", "kPa", 20],
  boost_act_kpa: ["Charge pressure (abs)", "kPa", 20],
  boost_cmd_kpa: ["Charge pressure cmd (abs)", "kPa", 20],
  timing_deg: ["Ignition timing", "°", 2],
  lambda: ["Lambda actual", "", 0.02],
  lambda_cmd: ["Lambda commanded", "", 0.02],
  stft_pct: ["Short-term fuel trim", "%", 2],
  ltft_pct: ["Long-term fuel trim", "%", 2],
  o2s1_ma: ["Upstream O2 current", "mA", 0.1],
  o2s2_v: ["Downstream O2", "V", 0.1],
  o2s2_trim_pct: ["Downstream O2 trim", "%", 2],
  lt_o2s2_trim_pct: ["Downstream O2 LT trim", "%", 2],
  rail_kpa: ["Fuel rail pressure", "kPa", 2000],
  coolant_c: ["Coolant temp", "°C", 5],
  iat_c: ["Intake air temp", "°C", 5],
  ambient_c: ["Ambient temp", "°C", 5],
  cat_temp_c: ["Catalyst temp", "°C", 50],
  volts: ["Module voltage", "V", 0.25],
  baro_kpa: ["Barometric pressure", "kPa", 1],
  fuel_pct: ["Fuel level", "%", 10],
  run_time_s: ["Engine run time", "s", 60],
  boost_psi: ["Boost (gauge)", "psi", 2],
  trim_total: ["Total fuel trim", "%", 2],
  lambda_err: ["Lambda error (act - cmd)", "", 0.01],
  rail_bar: ["Fuel rail pressure", "bar", 20],
};
// event kind -> label: carpi_stats.DETECTORS (tests/test_stats.py keeps them equal)
const AN_EVENTS = {
  pull: "Hard acceleration", // event
  timing_dip: "Timing ≤ -5° under boost", // event
  lean_boost: "Lean under boost", // event
  low_volts: "Voltage below 12.4 V", // event
  high_trim: "Fuel trim beyond ±12%", // event
  long_idle: "Idle longer than 3 min", // event
};
// Coloured both ways from 0 (blue below, red above); everything else low-to-high.
const AN_DIVERGING = new Set(["trim_total", "stft_pct", "ltft_pct", "lambda_err", "o2s2_trim_pct", "lt_o2s2_trim_pct"]);

const anSaved = store("carpi.anPreset");
const an = {
  drive: "live", entry: null,        // "live" or a saved drive id; its list entry
  info: null, infoAt: 0,             // the last "analysis" reply
  map: null, mapAt: 0, mapKey: "",   // the last "analysis_map" reply, and what it was for
  preset: anSaved === "custom" || (anSaved && anSaved in AN_PRESETS) ? anSaved : "trim",
  custom: { ...AN_PRESETS.trim, label: "Custom", ...(store("carpi.anCustom") || {}) },
  picker: false, list: [], more: false, current: null,
  cell: null, busy: false,
};

const anSupported = () => num(app.protocol) && app.protocol >= 6;
const anSpec = () => (an.preset === "custom" ? an.custom : AN_PRESETS[an.preset]);
const anCol = (c) => ({ label: AN_COLS[c] ? AN_COLS[c][0] : c, unit: AN_COLS[c] ? AN_COLS[c][1] : "" });

function anKey() {
  const s = anSpec();
  return [an.drive, s.x, s.y, s.z, [...s.filters].sort().join(","), s.stat].join("|");
}

// ---- polling: one request per tick, only while the tab is on screen ----

async function anTick() {
  if (app.tab !== "analysis" || document.hidden || !connected() || !anSupported() || an.busy) return;
  const now = performance.now(), live = an.drive === "live";
  const due = (r, at) => !r || r.state === "loading" || (live && now - at >= AN_LIVE_EVERY_MS);
  let req = null;
  if (due(an.info, an.infoAt)) req = ["analysis", { drive: an.drive }];
  else if (an.info.state === "ready" && (an.mapKey !== anKey() || due(an.map, an.mapAt))) {
    const s = anSpec();
    req = ["analysis_map", { drive: an.drive, x: s.x, y: s.y, z: s.z, filters: s.filters, stat: s.stat }];
  }
  if (!req) return;
  const key = anKey(), drive = an.drive;
  an.busy = true;
  try {
    const r = await app.link.request(...req);
    if (drive !== an.drive) return;                    // another drive was picked meanwhile
    if (req[0] === "analysis") { an.info = r.analysis; an.infoAt = performance.now(); }
    else if (key === anKey()) { an.map = r.map; an.mapAt = performance.now(); an.mapKey = key; }
  } catch (e) {
    if (drive !== an.drive) return;
    const err = { state: "error", error: e.message };
    if (req[0] === "analysis") { an.info = err; an.infoAt = performance.now(); }
    else { an.map = err; an.mapAt = performance.now(); an.mapKey = key; }
  } finally {
    an.busy = false;
    renderAnalysis();
  }
}

setInterval(anTick, AN_TICK_MS);

function anSelect(drive, entry) {
  Object.assign(an, { drive, entry, info: null, map: null, mapKey: "", cell: null, picker: false });
  renderAnalysis();
  anTick();
}

async function anLoadDrives(older) {
  try {
    const args = older && an.list.length ? { before: an.list[an.list.length - 1].id } : {};
    const r = await app.link.request("drives", args);
    an.list = older ? an.list.concat(r.drives) : r.drives;
    an.more = r.more;
    an.current = r.current;
    an.picker = true;
  } catch (e) { toast(e.message, true); }
  renderAnalysis();
}

// ---- rendering ----

// innerHTML only when it changed: rebuilding mid-tap would eat the tap.
function anHTML(el, html) {
  if (el.anHtml !== html) { el.innerHTML = html; el.anHtml = html; }
}

function anHint() {
  if (!connected()) return "Connect to CarPi to see the analysis of the drive being recorded, or of a saved drive.";
  if (app.protocol !== null && !anSupported()) {
    return "The CarPi software on this Pi is older than this page and can't analyze drives yet. "
      + "In an SSH session on the Pi: cd ~/carpi && git pull && sudo systemctl restart carpi-ble";
  }
  return "";
}

function renderAnalysis() {
  if (app.tab !== "analysis") return;
  const hint = anHint();
  $("#an-hint").textContent = hint;
  $("#an-hint").hidden = !hint;
  const on = connected() && anSupported();
  for (const id of ["#an-drive-card", "#an-summary-card", "#an-map-card", "#an-events-card"]) $(id).classList.toggle("is-off", !on);
  $("#an-change").disabled = !on;
  renderAnDrive();
  renderAnSummary();
  renderAnMap();
  renderAnEvents();
}

function renderAnDrive() {
  const a = an.info, e = an.entry;
  const length = (x) => `${fmtDuration(x.duration_s || 0)} · ${(x.rows || 0).toLocaleString()} rows`;
  let line = "—", sub = "";
  if (an.drive === "live") {
    if (!a || a.state === "loading") line = connected() ? "Loading…" : "—";
    else if (a.state === "none") { line = "No drive yet"; sub = "Start the engine: the drive being recorded shows here."; }
    else if (a.state === "error") { line = "Can't read this drive"; sub = a.error || ""; }
    else {
      line = `${a.recording ? "Being recorded" : "Last drive, ended"} · ${length(a)}`;
      sub = [a.recording && num(a.updated_s) ? `updated ${ago(a.updated_s)}` : null, a.id].filter(Boolean).join(" · ");
    }
  } else {
    line = e ? `${e.day} ${e.time}` : an.drive;
    if (a && a.state === "ready") line += ` · ${length(a)}`;
    sub = !a || a.state === "loading" ? "Reading the drive on the Pi…" : a.state === "error" ? a.error || ""
      : [e && e.tag ? `tag ${e.tag}` : null, e && e.private ? "private" : null,
        e && !e.complete ? "incomplete (power cut)" : null].filter(Boolean).join(" · ");
  }
  $("#an-drive-line").textContent = line;
  $("#an-drive-sub").textContent = sub;
  $("#an-change").textContent = an.picker ? "Close" : "Change";
  $("#an-picker").hidden = !an.picker;
  $("#an-back-live").hidden = an.drive === "live";
  anHTML($("#an-drives"), an.list.map((d) => {
    const badges = (d.id === an.current ? ' <span class="badge active">recording</span>' : "")
      + (d.private ? ' <span class="badge">private</span>' : "") + (d.complete ? "" : ' <span class="badge">incomplete</span>');
    const meta = [num(d.duration_s) ? fmtDuration(d.duration_s) : null, num(d.rows) ? `${d.rows.toLocaleString()} rows` : null,
      d.tag, d.preset].filter(Boolean).map(esc).join(" · ");
    return `<li><button type="button" data-id="${esc(d.id)}" class="${d.id === an.drive ? "on" : ""}">
      ${esc(d.day)} ${esc(d.time)}${badges}<br><span class="meta">${meta}</span></button></li>`;
  }).join("") || `<li class="sub">No saved drives on the Pi yet.</li>`);
  $("#an-older").hidden = !an.more;
}

function renderAnSummary() {
  const a = an.info && an.info.state === "ready" ? an.info : null;
  const s = a ? a.summary : null;
  const pct = (v) => (num(v) ? `${v > 0 ? "+" : ""}${v.toFixed(1)}%` : "—");
  anHTML($("#an-bands"), !s ? "" : "<tr><th>Fuel trim, warm and closed loop</th><th>Total</th><th>Time</th></tr>"
    + s.trim_bands.map((b) => `<tr><td>${esc(b.label)}</td><td>${pct(b.total_pct)}</td>`
      + `<td>${b.seconds ? fmtDuration(b.seconds) : "—"}</td></tr>`).join(""));
  const mph = app.units.speed === "mph";
  const speed = (k) => (num(k) ? (mph ? `${(k * 0.621371).toFixed(0)} mph` : `${k.toFixed(0)} km/h`) : "—");
  const temp = (c) => (num(c) ? `${showTemp(c).toFixed(0)} ${tempUnit()}` : "—");
  const at = (rpm) => (num(rpm) ? ` at ${rpm.toLocaleString()} rpm` : "");
  const rows = !s ? [] : [
    ["Distance", mph ? `${(s.distance_km * 0.621371).toFixed(1)} mi` : `${s.distance_km.toFixed(1)} km`],
    ["Top speed", speed(s.max_speed_kph)],
    ["Warm idle", num(s.idle_rpm) ? `${s.idle_rpm.toLocaleString()} rpm` : "—"],
    ["Peak boost", s.peak_boost ? `${s.peak_boost.psi.toFixed(1)} psi${at(s.peak_boost.rpm)}` : "—"],
    ["Lowest timing under boost", s.min_timing_boost ? `${s.min_timing_boost.deg.toFixed(1)}°${at(s.min_timing_boost.rpm)}` : "no boost over 5 psi"],
    ["Hottest intake air", temp(s.max_iat_c)],
    ["Hottest coolant", temp(s.max_coolant_c)],
  ];
  anHTML($("#an-kv"), rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join(""));
}

function anNum(v) {                      // axis numbers: 1500 -> "1.5k", 0.02 -> "0.02"
  if (Math.abs(v) >= 1000) return `${+(v / 1000).toFixed(1)}k`;
  return String(+v.toFixed(2));
}

function anDecimals(range, stat) {
  if (stat === "rows") return 0;
  return range >= 10 ? 0 : range >= 1 ? 1 : range >= 0.1 ? 2 : 3;
}

function anRange(m) {
  const vals = m.cells.map((c) => c[2]);
  return [Math.min(...vals), Math.max(...vals)];
}

function anGrid(m, spec) {
  const cells = new Map(m.cells.map((c) => [`${c[0]},${c[1]}`, c]));
  const [lo, hi] = anRange(m);
  const both = AN_DIVERGING.has(spec.z) && m.stat !== "rows";
  const big = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
  const shade = (v) => (both
    ? `color-mix(in srgb, ${v < 0 ? "var(--cold)" : "var(--warn)"} ${Math.round(Math.min(1, Math.abs(v) / big) * 70)}%, transparent)`
    : `color-mix(in srgb, var(--accent) ${hi > lo ? Math.round(((v - lo) / (hi - lo)) * 62) + 8 : 40}%, transparent)`);
  const dec = anDecimals(hi - lo, m.stat);
  const edge = (ax, i) => anNum(ax.start + i * ax.step);
  const every = m.nx > 8 ? 2 : 1;
  let html = `<div class="hm-grid" style="grid-template-columns: auto repeat(${m.nx}, minmax(0, 1fr))">`;
  for (let iy = m.ny - 1; iy >= 0; iy--) {                 // highest at the top, like an ECU table
    html += `<div class="hm-y">${edge(m.y, iy)}</div>`;
    for (let ix = 0; ix < m.nx; ix++) {
      const c = cells.get(`${ix},${iy}`);
      const sel = an.cell && an.cell[0] === ix && an.cell[1] === iy;
      html += c ? `<button type="button" class="hm-c${sel ? " sel" : ""}" data-c="${ix},${iy}" style="background:${shade(c[2])}">${c[2].toFixed(dec)}</button>`
        : '<div class="hm-c empty"></div>';
    }
  }
  html += "<div></div>" + Array.from({ length: m.nx }, (_, ix) => `<div class="hm-x">${ix % every ? "" : edge(m.x, ix)}</div>`).join("");
  return `${html}</div>`;
}

function anMapNote(spec, m) {
  // "engine speed (rpm)", but "boost (gauge), psi" where the label has brackets already
  const named = (c) => {
    const { label, unit } = anCol(c);
    return label.toLowerCase() + (!unit ? "" : label.includes("(") ? `, ${unit}` : ` (${unit})`);
  };
  const rows = spec.filters.map((f) => (AN_FILTERS.find((a) => a[0] === f) || [f, f])[1].toLowerCase()).join(", ");
  return `${AN_STATS[m.stat]} ${named(spec.z)} per cell. Across: ${named(spec.x)}; up: ${named(spec.y)}. Rows: ${rows || "all"}.`;
}

function anCellText(m, spec) {
  const c = an.cell && m.cells.find((x) => x[0] === an.cell[0] && x[1] === an.cell[1]);
  if (!c) return "Tap a cell for its numbers.";
  const x = anCol(spec.x), y = anCol(spec.y), z = anCol(spec.z);
  const span = (ax, i) => `${anNum(ax.start + i * ax.step)}–${anNum(ax.start + (i + 1) * ax.step)}`;
  const [lo, hi] = anRange(m);
  const v = c[2].toFixed(Math.min(3, anDecimals(hi - lo, m.stat) + 1));      // one more decimal than the grid
  const value = m.stat === "rows" ? `${c[2].toLocaleString()} rows` : `${AN_STATS[m.stat].toLowerCase()} ${v}${z.unit ? ` ${z.unit}` : ""}, ${c[3].toLocaleString()} rows`;
  return `${x.label} ${span(m.x, c[0])}${x.unit ? ` ${x.unit}` : ""}, ${y.label.toLowerCase()} ${span(m.y, c[1])}${y.unit ? ` ${y.unit}` : ""}: ${value}`;
}

function renderAnCustom() {
  const cols = (an.info && an.info.columns) || [];
  const s = an.custom;
  const opts = (sel) => cols.map((c) => `<option value="${esc(c)}"${c === sel ? " selected" : ""}>${esc(anCol(c).label)}</option>`).join("");
  anHTML($("#an-x"), opts(s.x));
  anHTML($("#an-y"), opts(s.y));
  anHTML($("#an-z"), opts(s.z));
  anHTML($("#an-stat"), Object.entries(AN_STATS).map(([id, label]) => `<option value="${id}"${id === s.stat ? " selected" : ""}>${label}</option>`).join(""));
  anHTML($("#an-filters"), AN_FILTERS.map(([id, label]) =>
    `<button class="vchip" type="button" data-filter="${id}" aria-pressed="${s.filters.includes(id)}">${label}</button>`).join(""));
}

function renderAnMap() {
  const choices = Object.entries(AN_PRESETS).map(([id, p]) => [id, p.label]).concat([["custom", "Custom"]]);
  anHTML($("#an-presets"), choices.map(([id, label]) =>
    `<button class="vchip" type="button" data-preset="${id}" aria-pressed="${an.preset === id}">${esc(label)}</button>`).join(""));
  $("#an-custom").hidden = an.preset !== "custom";
  if (an.preset === "custom") renderAnCustom();
  const spec = anSpec();
  const m = an.mapKey === anKey() ? an.map : null;
  const ready = an.info && an.info.state === "ready";
  chip($("#an-map-chip"), "", !ready || !m ? "—" : m.state === "loading" ? "Building…"
    : m.state === "ready" ? `${(m.rows || 0).toLocaleString()} rows` : m.state === "error" ? "Error" : "—");
  let note = "", html = "", cell = "";
  if (ready) {
    if (!m || m.state === "loading") note = "Building the map on the Pi…";
    else if (m.state === "error") note = m.error || "Couldn't build this map.";
    else if (m.state === "ready" && !m.cells.length) note = "No cells with 5 or more rows match these filters yet.";
    else if (m.state === "ready") { note = anMapNote(spec, m); html = anGrid(m, spec); cell = anCellText(m, spec); }
  }
  $("#an-map-note").textContent = note;
  anHTML($("#an-map"), html);
  $("#an-cell").textContent = cell;
}

function renderAnEvents() {
  const a = an.info && an.info.state === "ready" ? an.info : null;
  const evs = a ? [...a.events].reverse() : [];
  const worst = evs.some((e) => e.level === "crit") ? "crit" : evs.some((e) => e.level === "warn") ? "warn" : "";
  chip($("#an-events-chip"), worst, a ? String(a.events_total) : "—");
  const more = a ? a.events_total - a.events.length : 0;
  anHTML($("#an-events"), !a ? "" : evs.length ? evs.map((e) => `
    <li class="${esc(e.level)}"><span class="t">${fmtDuration(e.t0)}</span>
      <span><strong>${esc(AN_EVENTS[e.kind] || e.kind)}</strong>${e.ongoing ? ' <span class="badge active">now</span>' : ""}
      <br><span class="meta">${esc(e.text)}</span></span></li>`).join("")
    + (more > 0 ? `<li class="sub">+${more} earlier</li>` : "")
    : `<li class="sub">Nothing flagged${a.recording ? " so far" : ""}.</li>`);
}

// ---- handlers ----

$("#an-change").addEventListener("click", () => {
  if (an.picker) { an.picker = false; renderAnalysis(); } else anLoadDrives(false);
});
$("#an-older").addEventListener("click", () => anLoadDrives(true));
$("#an-back-live").addEventListener("click", () => anSelect("live", null));
$("#an-drives").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-id]");
  if (!b) return;
  const d = an.list.find((x) => x.id === b.dataset.id) || null;
  if (d && d.id === an.current) anSelect("live", null);               // the drive being recorded
  else anSelect(b.dataset.id, d);
});
$("#an-presets").addEventListener("click", (e) => {
  const b = e.target.closest("[data-preset]");
  if (!b) return;
  an.preset = b.dataset.preset;
  store("carpi.anPreset", an.preset);
  an.cell = null;
  renderAnalysis();
  anTick();
});
function anCustomChanged() {
  store("carpi.anCustom", an.custom);
  an.cell = null;
  renderAnalysis();
  anTick();
}
$("#an-filters").addEventListener("click", (e) => {
  const b = e.target.closest("[data-filter]");
  if (!b) return;
  const f = b.dataset.filter, s = an.custom;
  s.filters = s.filters.includes(f) ? s.filters.filter((x) => x !== f) : s.filters.concat(f);
  anCustomChanged();
});
for (const [id, key] of [["#an-x", "x"], ["#an-y", "y"], ["#an-z", "z"], ["#an-stat", "stat"]]) {
  $(id).addEventListener("change", (e) => { an.custom[key] = e.target.value; anCustomChanged(); });
}
$("#an-map").addEventListener("click", (e) => {
  const b = e.target.closest("[data-c]");
  if (!b) return;
  an.cell = b.dataset.c.split(",").map(Number);
  renderAnMap();
});

// ---- demo: DemoLink (config.js) answers the analysis requests with made-up drives ----

const AN_DEMO_SAVED = Array.from({ length: 20 }, (_, i) => {
  const day = new Date(Date.UTC(2026, 10, 2) - Math.floor(i / 2) * 86400000).toISOString().slice(0, 10);
  const hhmm = i % 2 ? "0815" : "1742";
  const dur = 600 + ((i * 397) % 2400);
  return { id: `${day}/drive-${hhmm}02`, day, time: `${hhmm.slice(0, 2)}:${hhmm.slice(2)}`, duration_s: dur,
    rows: Math.round(dur * 7.7), tag: ["post-pcv", "post-pcv", "baseline"][i % 3], preset: "Full (default)",
    complete: i !== 3, private: i === 5 };
});

function anDemoAnalysis(id, dur, rows, live) {
  const events = [
    { kind: "long_idle", level: "info", t0: 20, t1: 230, text: "3.5 min, trim +8.3%", ongoing: false },
    { kind: "pull", level: "info", t0: 610, t1: 616.4, text: "2,100→5,600 rpm, peak 15.8 psi, min timing 2.5°, min λ 0.832 (shifts excluded)", ongoing: false },
    { kind: "timing_dip", level: "warn", t0: 1190.2, t1: 1190.6, text: "min -6.0° at 3,350 rpm, 14.1 psi", ongoing: false },
  ].filter((e) => e.t1 < dur);
  const band = (label, share, stft, ltft) => ({ label, seconds: Math.round(dur * share), stft_pct: stft, ltft_pct: ltft, total_pct: +(stft + ltft).toFixed(2) });
  return {
    id, live, recording: live, state: "ready", error: null, rows, duration_s: dur, updated_s: live ? 0.6 : undefined,
    columns: Object.keys(AN_COLS).filter((c) => c !== "boost_cmd_kpa"),
    summary: {
      distance_km: +((dur / 3600) * 42).toFixed(2), max_speed_kph: dur > 300 ? 104 : 38, idle_rpm: 812,
      trim_bands: [band("Idle, under 1,000", 0.22, 0.8, 7.4), band("1,000–1,800", 0.3, 0.2, 3.1),
        band("1,800–2,500", 0.28, -0.3, 0.6), band("2,500 and up", 0.08, -0.6, 0.1)],
      peak_boost: dur > 620 ? { psi: 15.8, rpm: 3950, pedal: 61 } : null,
      min_timing_boost: dur > 620 ? { deg: -6, rpm: 3350 } : null,
      max_iat_c: 31, max_coolant_c: Math.min(97, Math.round(20 + dur * 0.4)),
    },
    events, events_total: events.length,
  };
}

function anDemoMap(id, a) {
  const step = (c) => (AN_COLS[c] ? AN_COLS[c][2] : 1);
  const nx = 9, ny = 8, cells = [];
  for (let ix = 0; ix < nx; ix++) {
    for (let iy = 0; iy < ny; iy++) {
      if (ix + iy > 13 || (ix > 5 && iy < 2)) continue;           // corners a car never visits
      const r = ix / (nx - 1), l = iy / (ny - 1);
      const n = Math.round(6 + 500 * (1 - r) * (1 - l) ** 2);
      const v = { trim_total: 8.6 * (1 - r) ** 2 * (1 - l) - 0.4, timing_deg: 4 + 24 * r - 16 * l,
        lambda_err: 0.012 * (r - l), boost_psi: Math.max(-1, 17 * l * r - 1.5) }[a.z] ?? 10 + 30 * r + 15 * l;
      cells.push([ix, iy, a.stat === "rows" ? n : +v.toFixed(3), n]);
    }
  }
  return { id, state: "ready", x: { col: a.x, step: step(a.x), start: a.x === "rpm" ? 500 : 0 },
    y: { col: a.y, step: step(a.y), start: 0 }, nx, ny, stat: a.stat, cells, rows: cells.reduce((s, c) => s + c[3], 0) };
}

DemoLink.prototype.analysisRequest = function (cmd, args, fail) {
  const st = this.status();
  const liveId = st.drive ? st.drive.replace(/\.csv$/, "") : null;
  const pick = (d) => (d === undefined || d === "live" ? liveId : d);
  const saved = (id) => AN_DEMO_SAVED.find((x) => x.id === id);
  const check = (id) => id === liveId || saved(id) || fail("no such drive");
  const loading = (id) => {                                     // a saved drive takes a moment to read
    if (id === liveId) return false;
    this.anAsked = this.anAsked || {};
    this.anAsked[id] = this.anAsked[id] || Date.now();
    return Date.now() - this.anAsked[id] < 800;
  };
  switch (cmd) {
    case "drives": {
      const from = args.before ? AN_DEMO_SAVED.findIndex((x) => x.id === args.before) + 1 : 0;
      return { ok: true, drives: AN_DEMO_SAVED.slice(from, from + 15), more: from + 15 < AN_DEMO_SAVED.length, current: liveId };
    }
    case "analysis": {
      const id = pick(args.drive);
      if (!id) return { ok: true, analysis: { id: null, live: true, recording: false, state: "none" } };
      check(id);
      if (loading(id)) return { ok: true, analysis: { id, live: false, recording: false, state: "loading" } };
      const s = saved(id);
      return { ok: true, analysis: id === liveId ? anDemoAnalysis(id, st.elapsed_s, st.rows, true) : anDemoAnalysis(id, s.duration_s, s.rows, false) };
    }
    case "analysis_map": {
      const id = pick(args.drive);
      if (!id) return { ok: true, map: { id: null, state: "none" } };
      check(id);
      if (loading(id)) return { ok: true, map: { id, state: "loading" } };
      return { ok: true, map: anDemoMap(id, args) };
    }
    default: return null;
  }
};

renderAnalysis();                        // config.js restored the tab before this file loaded
