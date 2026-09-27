// CarPi drive viewer. Data comes pre-analyzed from analysis/build_site.py;
// this file only renders it. No analysis happens in the browser.

const SIGNALS = {
  speed_mph:  { name: "Speed",           unit: "mph", digits: 0 },
  rpm:        { name: "Engine speed",    unit: "rpm", digits: 0 },
  boost_psi:  { name: "Boost",           unit: "psi", digits: 1 },
  trim_pct:   { name: "Total fuel trim", unit: "%",   digits: 1, zero: true },
  load_pct:   { name: "Engine load",     unit: "%",   digits: 0 },
  timing_deg: { name: "Ignition timing", unit: "°",   digits: 1, zero: true },
  lambda:     { name: "Lambda",          unit: "",    digits: 3 },
  pedal_pct:  { name: "Pedal",           unit: "%",   digits: 0 },
  rail_bar:   { name: "Fuel rail",       unit: "bar", digits: 0 },
  coolant_c:  { name: "Coolant",         unit: "°C",  digits: 0 },
};
const DEFAULT_SIGNALS = ["speed_mph", "rpm", "boost_psi", "trim_pct"];
const PANEL_HEIGHT = 118;
const UNIT_GAP = (unit) => (unit === "" || unit === "%" || unit === "°" ? "" : " ");
const AXIS_HEIGHT = 28;

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const num = (v, digits = 0) =>
  new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
const signed = (v, digits = 1) => (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), digits);
const fmtDate = (iso) =>
  new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const fmtClock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  node.append(...children.filter((c) => c != null));
  return node;
};

const state = {
  site: null,          // drives.json
  cache: new Map(),    // id -> per-drive json
  driveId: null,
  signals: [...DEFAULT_SIGNALS],
  steady: true,
  plots: [],
};

// ---------- data ----------

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

async function loadDrive(id) {
  if (!state.cache.has(id)) state.cache.set(id, await getJSON(`data/${id}.json`));
  return state.cache.get(id);
}

const currentMeta = () => state.site.drives.find((d) => d.id === state.driveId);
const currentDrive = () => state.cache.get(state.driveId);   // undefined while its JSON loads

// ---------- hero ----------

function renderHero(meta, drive) {
  const s = meta.summary;
  const steady = drive.bands.steady;
  const elsewhere = Math.max(...steady.slice(1).map((b) => Math.abs(b.total_pct ?? 0)));
  const tiles = [
    { label: "Distance", value: `${num(s.distance_mi, 1)} mi`, sub: `${num(s.duration_s / 60, 1)} min logged` },
    { label: "Samples", value: num(s.samples), sub: `${num(s.samples / s.duration_s, 1)} per second` },
    { label: "Peak boost", value: s.peak_boost_psi == null ? "–" : `${num(s.peak_boost_psi, 1)} psi`,
      sub: s.peak_boost_rpm == null ? "" : `at ${num(s.peak_boost_rpm)} rpm` },
    { label: "Idle fuel trim", value: s.idle_trim_pct == null ? "–" : `${signed(s.idle_trim_pct)}%`,
      sub: `${num(elsewhere, 1)}% or less while driving`, accent: Math.abs(s.idle_trim_pct ?? 0) >= 3 },
  ];
  $("stats").replaceChildren(...tiles.map((t) =>
    el("div", { class: "stat" },
      el("div", { class: "stat-label", text: t.label }),
      el("div", { class: `stat-value${t.accent ? " stat-value--accent" : ""}`, text: t.value }),
      el("div", { class: "stat-sub", text: t.sub }))));
  $("finding").textContent = meta.finding;
  $("finding").hidden = !meta.finding;
}

function renderStatus() {
  const drives = state.site.drives;
  const latest = drives.map((d) => d.date).sort().at(-1);
  const miles = drives.reduce((sum, d) => sum + d.summary.distance_mi, 0);
  $("status-text").textContent =
    `${drives.length} drive${drives.length === 1 ? "" : "s"} published · ${num(miles, 1)} mi logged · latest ${fmtDate(latest)}`;
}

// ---------- drive cards ----------

function driveCard(d) {
  const btn = el("button", { class: "drive", type: "button", "aria-pressed": String(d.id === state.driveId) },
    // a grouped card's heading already says what the tag would
    el("span", { class: "drive-title", text: d.title }, d.tag && !d.group ? el("span", { class: "badge", text: d.tag }) : null),
    el("span", { class: "drive-meta", text: `${fmtDate(d.date)} · ${num(d.summary.duration_s / 60, 1)} min · ${num(d.summary.distance_mi, 1)} mi` }),
    d.note ? el("span", { class: "drive-note", text: d.note }) : null);
  btn.addEventListener("click", () => selectDrive(d.id, true));
  return btn;
}

function driveGroup(label, cards) {
  return el("div", { class: "drive-group", role: "group", "aria-label": label || "Drives" },
    label ? el("h3", { class: "drive-group-label", text: label }) : null,
    el("div", { class: "drives" }, ...cards));
}

function renderDriveCards() {
  const groups = new Map();   // manifest order, drives with the same group side by side
  for (const d of state.site.drives) {
    if (!groups.has(d.group)) groups.set(d.group, []);
    groups.get(d.group).push(driveCard(d));
  }
  const blocks = [...groups].map(([label, cards]) => driveGroup(label, cards));
  if (state.site.upcoming) {
    blocks.push(driveGroup(state.site.upcoming, [
      el("div", { class: "drive drive--upcoming", "aria-disabled": "true" },
        el("span", { class: "drive-title", text: "Coming soon" }),
        el("span", { class: "drive-note", text: "Logged after the repair, for a before/after comparison." }))]));
  }
  $("drives").replaceChildren(...blocks);
}

// ---------- signal trace ----------

function renderSignalChips() {
  $("signals").replaceChildren(...Object.entries(SIGNALS).map(([key, sig]) => {
    const chip = el("button", { class: "chip", type: "button", "aria-pressed": String(state.signals.includes(key)), text: sig.name });
    chip.addEventListener("click", () => {
      const on = state.signals.includes(key);
      if (on && state.signals.length === 1) return;   // keep at least one panel
      state.signals = on ? state.signals.filter((k) => k !== key)
                         : Object.keys(SIGNALS).filter((k) => k === key || state.signals.includes(k));
      renderSignalChips();
      const drive = currentDrive();
      if (drive) renderTrace(drive);   // still loading: selectDrive renders with the new signals
    });
    return chip;
  }));
}

function setReadout(trace, idx) {
  const out = $("readout");
  if (idx == null) { out.innerHTML = "&nbsp;"; return; }
  out.replaceChildren(el("span", { text: `${fmtClock(trace.t[idx])}  ` }), ...state.signals.flatMap((key, i) => {
    const v = trace[key][idx];
    const sig = SIGNALS[key];
    const value = v == null ? "–" : `${num(v, sig.digits)}${UNIT_GAP(sig.unit)}${sig.unit}`;
    return [i ? el("span", { text: " · " }) : null, `${sig.name.toLowerCase()} ${value}`];
  }).filter(Boolean));
}

function renderTrace(drive) {
  state.plots.forEach((p) => p.destroy());
  state.plots = [];
  const box = $("trace");
  box.replaceChildren();
  const { trace } = drive;
  const width = box.clientWidth;
  const colors = { accent: css("--accent"), text: css("--text"), line: css("--line"), muted: css("--muted-bar") };
  let syncing = false;

  state.signals.forEach((key, i) => {
    const sig = SIGNALS[key];
    const last = i === state.signals.length - 1;
    const panel = el("div", { class: "panel", role: "img",
      "aria-label": `${sig.name} over the drive${sig.unit ? `, in ${sig.unit}` : ""}` },
      el("div", { class: "panel-title" }, `${sig.name} `, el("span", { text: sig.unit })));
    box.append(panel);
    const opts = {
      width,
      height: PANEL_HEIGHT + (last ? AXIS_HEIGHT : 0),
      padding: [18, 8, last ? 0 : 8, 0],
      legend: { show: false },
      cursor: {
        sync: { key: "carpi" },
        y: false,
        drag: { x: true, y: false, setScale: true },
        points: { size: 7, fill: colors.accent, stroke: colors.accent },
      },
      scales: { x: { time: false } },
      series: [{}, { stroke: colors.accent, width: 1.6, spanGaps: false, points: { show: false } }],
      axes: [
        { show: last, stroke: colors.text, grid: { show: false }, ticks: { show: false }, size: AXIS_HEIGHT,
          incrs: [15, 30, 60, 120, 180, 300, 600, 900, 1800, 3600],
          font: `11px ${css("--mono")}`, values: (u, vals) => vals.map(fmtClock) },
        { stroke: colors.text, grid: { stroke: colors.line, width: 1 }, ticks: { show: false }, size: 52,
          font: `11px ${css("--mono")}`, values: (u, vals) => vals.map((v) => num(v, Math.abs(v) < 10 && v % 1 ? 1 : 0)) },
      ],
      hooks: {
        setCursor: [(u) => setReadout(trace, u.cursor.idx)],
        setScale: [(u, scaleKey) => {
          if (scaleKey !== "x" || syncing) return;
          syncing = true;
          const { min, max } = u.scales.x;
          state.plots.forEach((p) => p !== u && p.setScale("x", { min, max }));
          syncing = false;
        }],
        draw: sig.zero ? [(u) => {
          const y = Math.round(u.valToPos(0, "y", true));
          if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) return;
          const ctx = u.ctx;
          ctx.save();
          ctx.strokeStyle = colors.muted;
          ctx.setLineDash([4, 4]);
          ctx.beginPath();
          ctx.moveTo(u.bbox.left, y);
          ctx.lineTo(u.bbox.left + u.bbox.width, y);
          ctx.stroke();
          ctx.restore();
        }] : [],
      },
    };
    state.plots.push(new uPlot(opts, [trace.t, trace[key]], panel));
  });
}

// ---------- fuel trim bands ----------

function renderBands(drive) {
  const bands = drive.bands[state.steady ? "steady" : "all"];
  const all = [...drive.bands.steady, ...drive.bands.all].map((b) => b.total_pct ?? 0);
  const lo = Math.min(0, ...all);
  const hi = Math.max(10, ...all);
  const span = hi - lo;
  const zero = (-lo / span) * 100;
  $("trim-caption").textContent = state.steady
    ? "Warm engine, closed loop. Positive means the ECU is adding fuel to compensate; a healthy engine sits near zero in every band."
    : "Every moment the engine was running, including warm-up and enrichment, so the picture is noisier.";
  $("bands").replaceChildren(...bands.map((b) => {
    const v = b.total_pct;
    const track = el("div", { class: "band-track", style: `--zero:${zero}%` });
    if (v != null) {
      const w = (Math.abs(v) / span) * 100;
      const left = v >= 0 ? zero : zero - w;
      track.append(el("div", { class: `band-bar ${v >= 0 ? "pos" : "neg"}${Math.abs(v) >= 3 ? " hot" : ""}`,
        style: `left:${left}%;width:${Math.max(w, 0.6)}%` }));
    }
    return el("div", { class: `band${v == null ? " band--empty" : ""}` },
      el("div", { class: "band-label" }, `${b.label} rpm`, el("small", { text: `${num(b.seconds)} s in band` })),
      track,
      el("div", { class: "band-value", text: v == null ? "no data" : `${signed(v)}%` }));
  }));
}

// ---------- sensor checks ----------

function renderChecks(meta) {
  $("checks").replaceChildren(...meta.checks.map((c) => {
    const icon = c.pass === true ? ["✓", ""] : c.pass === false ? ["✕", " fail"] : ["–", " skip"];
    return el("li", { class: "check" },
      el("span", { class: `check-icon${icon[1]}`, "aria-label": c.pass === true ? "pass" : c.pass === false ? "fail" : "not run", text: icon[0] }),
      el("div", { class: "check-body" },
        el("span", { class: "check-name", text: c.name }),
        el("span", { class: "check-measured", text: c.measured }),
        el("span", { class: "check-ref", text: c.reference })));
  }));
}

// ---------- wiring ----------

// Created here rather than in index.html, so a cached older page still works with this script.
function driveError() {
  let node = $("drive-error");
  if (!node) {
    node = el("p", { class: "load-error", id: "drive-error", role: "alert" });
    node.hidden = true;
    $("drives").after(node);
  }
  return node;
}

async function selectDrive(id, updateHash) {
  const previous = state.driveId;
  state.driveId = id;
  renderDriveCards();
  let drive;
  try {
    drive = await loadDrive(id);
  } catch (err) {
    if (state.driveId !== id) return;   // a newer click already moved on
    if (!previous) throw err;           // first load: main() shows the page-level error
    console.error(err);
    state.driveId = previous;           // keep showing the drive that did load
    renderDriveCards();
    const title = state.site.drives.find((d) => d.id === id)?.title ?? "that drive";
    driveError().textContent = `Couldn't load “${title}”. Check your connection and try again.`;
    driveError().hidden = false;
    return;
  }
  if (state.driveId !== id) return;     // a newer click won
  driveError().hidden = true;
  if (updateHash) history.replaceState(null, "", `#${id}`);
  const meta = currentMeta();
  renderHero(meta, drive);
  renderTrace(drive);
  renderBands(drive);
  renderChecks(meta);
}

function showError(err) {
  console.error(err);
  $("status-text").textContent = "Couldn't load the drive data";
  $("content").replaceChildren(el("p", { class: "error",
    text: "The drive data didn't load. Try refreshing; if it keeps happening, the latest build may have failed." }));
}

async function main() {
  try {
    state.site = await getJSON("data/drives.json");
    const ids = state.site.drives.map((d) => d.id);
    const fromHash = decodeURIComponent(location.hash.slice(1));
    renderStatus();
    renderSignalChips();
    $("steady").addEventListener("change", (e) => {
      state.steady = e.target.checked;
      const drive = currentDrive();
      if (drive) renderBands(drive);   // still loading: selectDrive renders with the new setting
    });
    let width = $("trace").clientWidth;
    new ResizeObserver(() => {
      const box = $("trace");
      if (!box) return;   // showError() replaced the page content
      const w = box.clientWidth;
      if (w === width) return;
      width = w;
      state.plots.forEach((p) => p.setSize({ width: w, height: p.height }));
    }).observe($("trace"));
    await selectDrive(ids.includes(fromHash) ? fromHash : ids[0], false);
  } catch (err) {
    showError(err);
  }
}

main();
