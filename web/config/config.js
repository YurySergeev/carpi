// CarPi over Bluetooth LE (Web Bluetooth): a live dashboard of the link, the car's OBD-II port and
// the Pi itself, plus picking what the logger records (presets and tag).
// Protocol: newline-terminated JSON lines both ways; see carpi_control.py in the repo.
// ?demo (or the "Try the demo" button) swaps the Bluetooth link for a simulated CarPi.
// ?demo=unplugged simulates a Pi that's powered but not on the OBD port.
// ?debug shows a log of every request sent and every Bluetooth notification received.
"use strict";

const SERVICE = "6c2a0001-4f3b-4d1e-9a57-3c8e2b7d5f10";
const RX = "6c2a0002-4f3b-4d1e-9a57-3c8e2b7d5f10";   // we write requests
const TX = "6c2a0003-4f3b-4d1e-9a57-3c8e2b7d5f10";   // CarPi notifies replies
const CHUNK = 180;              // bytes per write; longer writes are split by the phone's BLE stack
const RETRY_AFTER_MS = 6000;    // no reply by then: subscribe to replies again and resend once
const REPLY_TIMEOUT_MS = 10000;
const STATUS_EVERY_MS = 2000;   // carpi_ble.py samples the Pi every 2 s
const STALE_MS = 8000;          // no reply for this long: the link is "not responding"
const DEFAULT_REQ_PER_S = 100;  // measured on the A3: ~7.7 rows/s x 13 requests
const DEFAULT_HZ = 10;          // carpi_logger.py --hz
const NAME_OK = /^[\w .()&+-]{1,32}$/u;
const TAG_OK = /^[A-Za-z0-9._-]{1,32}$/;
const STATES = {
  waiting: "Waiting for the car",
  detecting: "Car awake, looking for the engine",
  ignition_off: "Car awake, engine off",
  offline: "Logger isn't running",
  unknown: "Logger status unknown",
};

const DEBUG = new URLSearchParams(location.search).has("debug");
const debugLines = [];
function dbg(text) {
  if (!DEBUG) return;
  const t = new Date();
  const stamp = `${t.toTimeString().slice(0, 8)}.${String(t.getMilliseconds()).padStart(3, "0")}`;
  debugLines.push(`${stamp} ${text}`);
  if (debugLines.length > 200) debugLines.shift();
  const pre = document.querySelector("#debug-log");
  if (pre) { pre.textContent = debugLines.join("\n"); pre.scrollTop = pre.scrollHeight; }
}

const enc = new TextEncoder();
const dec = new TextDecoder();
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key));
    localStorage.setItem(key, JSON.stringify(value));
  } catch { return null; }
}

// ---------- links ----------

class BleLink {
  constructor() {
    this.rid = 0;
    this.pending = new Map();
    this.buf = new Uint8Array(0);
    this.queue = Promise.resolve();
    this.onclose = null;
    this.onData = (e) => this.data(e.target.value);
  }

  get connected() { return !!(this.device && this.device.gatt.connected); }
  get name() { return (this.device && this.device.name) || "CarPi"; }

  async connect() {
    if (!this.device) {
      this.device = await navigator.bluetooth.requestDevice({ filters: [{ services: [SERVICE] }] });
      this.device.addEventListener("gattserverdisconnected", () => this.closed());
    }
    await this.device.gatt.connect();
    dbg(`GATT connected to ${this.name}`);
    await this.subscribe();                 // first encrypted access: Android shows the pairing prompt
    await this.write(enc.encode("\n"));     // flush a half line the Pi may hold from a dropped link
  }

  // (Re)look up the characteristics and subscribe to replies. Also used when a reply goes
  // missing: on the first connection after pairing Android re-reads the services, which can
  // leave the old subscription dead.
  async subscribe() {
    const service = await this.device.gatt.getPrimaryService(SERVICE);
    this.rx = await service.getCharacteristic(RX);
    const tx = await service.getCharacteristic(TX);
    if (this.tx) {
      this.tx.removeEventListener("characteristicvaluechanged", this.onData);
      if (this.tx === tx) { try { await tx.stopNotifications(); } catch { /* already off */ } }
    }
    this.tx = tx;
    tx.addEventListener("characteristicvaluechanged", this.onData);
    await tx.startNotifications();
    if (this.buf.length) dbg(`dropped ${this.buf.length} B of a half-received reply`);
    this.buf = new Uint8Array(0);           // a half-received reply is useless now
    dbg("subscribed to replies (TX notifications on)");
  }

  disconnect() { if (this.connected) this.device.gatt.disconnect(); }

  closed() {
    for (const p of this.pending.values()) p.reject(new Error("Disconnected"));
    this.pending.clear();
    if (this.onclose) this.onclose();
  }

  data(view) {
    const chunk = new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
    const merged = new Uint8Array(this.buf.length + chunk.length);
    merged.set(this.buf);
    merged.set(chunk, this.buf.length);
    this.buf = merged;
    dbg(`← notification ${chunk.length} B (${this.buf.length} B waiting for a newline)`);
    let i;
    while ((i = this.buf.indexOf(10)) >= 0) {
      const line = dec.decode(this.buf.slice(0, i));
      this.buf = this.buf.slice(i + 1);
      let msg;
      try { msg = JSON.parse(line); } catch {
        dbg(`✗ ${line.length}-char line isn't valid JSON: ${line.slice(0, 50)} … ${line.slice(-30)}`);
        continue;
      }
      const p = this.pending.get(msg.rid);
      if (!p) { dbg(`reply for rid ${msg.rid}, which isn't pending (already answered or timed out)`); continue; }
      dbg(`✓ reply to rid ${msg.rid}: ${line.length} chars, ok=${msg.ok}`);
      this.pending.delete(msg.rid);
      if (msg.ok) p.resolve(msg); else p.reject(new Error(msg.error || "CarPi refused"));
    }
  }

  async write(bytes) {
    const send = (this.rx.writeValueWithResponse || this.rx.writeValue).bind(this.rx);
    for (let i = 0; i < bytes.length; i += CHUNK) await send(bytes.slice(i, i + CHUNK));
  }

  request(cmd, args = {}) {
    const rid = ++this.rid;
    const line = enc.encode(JSON.stringify({ rid, cmd, ...args }) + "\n");
    dbg(`→ ${cmd} rid ${rid} (${line.length} B)`);
    return new Promise((resolve, reject) => {
      let timer;
      const arm = (ms, fn) => { clearTimeout(timer); timer = setTimeout(fn, ms); };
      this.pending.set(rid, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      // one request on the wire at a time, so chunks of two requests never interleave
      const send = () => {
        this.queue = this.queue.then(() => this.write(line)).catch((e) => {
          const p = this.pending.get(rid);
          if (p) { this.pending.delete(rid); p.reject(e); }
        });
      };
      // No reply in time: subscribe again and resend once. Every command is safe to repeat, and
      // both copies carry the same rid, so whichever reply arrives first wins.
      arm(RETRY_AFTER_MS, async () => {
        if (!this.pending.has(rid)) return;
        dbg(`… no reply to rid ${rid} after ${RETRY_AFTER_MS / 1000} s: resubscribing and resending`);
        try { await this.subscribe(); } catch { /* the resend may still get through */ }
        if (!this.pending.has(rid)) return;
        arm(REPLY_TIMEOUT_MS, () => {
          dbg(`✗ rid ${rid} gave up (${this.buf.length} B left without a newline)`);
          this.pending.delete(rid);
          reject(new Error("No reply from CarPi"));
        });
        send();
      });
      send();
    });
  }
}

// A stand-in CarPi that follows carpi_control.py, for trying the page without the car.
class DemoLink {
  constructor() {
    const catalog = [
      ["0C", "Engine speed", "rpm", "Engine", ["rpm"]], ["0B", "Manifold pressure", "kPa", "Air & boost", ["map_kpa"]],
      ["70", "Boost pressure", "kPa", "Air & boost", ["boost_cmd_kpa", "boost_act_kpa"]], ["04", "Calculated load", "%", "Engine", ["load_pct"]],
      ["0E", "Ignition timing", "°", "Engine", ["timing_deg"]], ["06", "Short-term fuel trim", "%", "Fueling", ["stft_pct"]],
      ["44", "Commanded lambda", "λ", "Fueling", ["lambda_cmd"]], ["34", "Lambda (wideband)", "λ", "Fueling", ["lambda", "o2s1_ma"]],
      ["23", "Fuel rail pressure", "kPa", "Fueling", ["rail_kpa"]], ["49", "Accelerator pedal", "%", "Driver", ["pedal_pct"]],
      ["11", "Throttle", "%", "Driver", ["throttle_pct"]], ["0D", "Vehicle speed", "km/h", "Driver", ["speed_kph"]],
      ["05", "Coolant temperature", "°C", "Temperatures", ["coolant_c"]], ["0F", "Intake air temperature", "°C", "Temperatures", ["iat_c"]],
      ["07", "Long-term fuel trim", "%", "Fueling", ["ltft_pct"]], ["42", "Module voltage", "V", "Other", ["volts"]],
      ["33", "Barometric pressure", "kPa", "Air & boost", ["baro_kpa"]], ["46", "Ambient temperature", "°C", "Temperatures", ["ambient_c"]],
      ["2F", "Fuel level", "%", "Other", ["fuel_pct"]], ["3C", "Catalyst temperature", "°C", "Temperatures", ["cat_temp_c"]],
      ["15", "Rear O2 sensor", "V", "Fueling", ["o2s2_v", "o2s2_trim_pct"]], ["56", "Long-term rear O2 trim", "%", "Fueling", ["lt_o2s2_trim_pct"]],
      ["43", "Absolute load", "%", "Engine", ["abs_load_pct"]], ["1F", "Engine run time", "s", "Other", ["run_time_s"]],
    ];
    const viewer = new Set(["0C", "0D", "06", "07", "05", "44", "70", "33"]);
    this.catalog = catalog.map(([id, label, unit, group, columns]) => ({ id, label, unit, group, columns, viewer: viewer.has(id) }));
    const ids = this.catalog.map((c) => c.id);
    const w = (pairs) => Object.fromEntries(ids.filter((id) => pairs[id]).map((id) => [id, pairs[id]]));
    this.builtin = [
      { id: "default", name: "Full (default)", weights: Object.fromEntries(ids.map((id, i) => [id, i < 12 ? 12 : 1])) },
      { id: "fuel-trim", name: "Fuel trim study", weights: w({ "0C": 12, "0B": 12, "70": 4, "04": 6, "06": 12, "44": 12, "34": 12, "11": 4, "0D": 4, "05": 1, "0F": 1, "07": 6, "33": 1, "15": 1 }) },
      { id: "pulls", name: "Pulls (boost & timing)", weights: w({ "0C": 12, "0B": 6, "70": 12, "04": 12, "0E": 12, "06": 3, "44": 12, "34": 12, "23": 12, "49": 12, "11": 12, "0D": 6, "05": 1, "0F": 1, "07": 1, "33": 1 }) },
    ];
    this.user = [];
    this.active = "default";
    this.tag = "post-pcv";
    this.supported = ids.filter((id) => id !== "2F");
    this.started = Date.now() - 95000;
    this.booted = Date.now() - 412000;
    this.unplugged = new URLSearchParams(location.search).get("demo") === "unplugged";
    this.connected = false;
    this.name = "CarPi (demo)";
    this.onclose = null;
  }

  async connect() { await new Promise((r) => setTimeout(r, 400)); this.connected = true; }
  disconnect() { this.connected = false; if (this.onclose) this.onclose(); }

  find(id) { return this.builtin.concat(this.user).find((p) => p.id === id); }

  status() {
    if (this.unplugged) return { state: "waiting", hz: DEFAULT_HZ, req_per_s: null };
    const preset = this.find(this.active);
    const elapsed = (Date.now() - this.started) / 1000;
    const rate = estimate(preset.weights, DEFAULT_REQ_PER_S, DEFAULT_HZ).rows;
    return {
      state: "logging", drive: "2026-10-06/drive-171502.csv", elapsed_s: Math.round(elapsed),
      rows: Math.round(elapsed * rate), rows_per_s: +rate.toFixed(2), req_per_s: DEFAULT_REQ_PER_S,
      hz: DEFAULT_HZ, preset: { id: preset.id, name: preset.name },
    };
  }

  // Same shape as carpi_system.Sampler.snapshot(), with a bit of wobble so the page looks alive.
  live() {
    const j = (x, d) => +(x + (Math.random() - 0.5) * 2 * d).toFixed(1);
    const busy = !this.unplugged;
    const mhz = busy ? (Math.random() < 0.7 ? 1800 : 1500) : (Math.random() < 0.8 ? 600 : 1800);
    return {
      status: this.status(),
      system: {
        cpu_temp_c: j(busy ? 54 : 47, 1.2), cpu_pct: j(busy ? 17 : 3, busy ? 5 : 1.5), cpu_mhz: mhz,
        cpu_max_mhz: 1800, cores: 4, load_1m: j(busy ? 0.62 : 0.08, 0.05), mem_pct: j(busy ? 23 : 18, 0.4),
        mem_total_mb: 1849, disk_free_gb: 22.71, disk_pct: 21.4,
        uptime_s: Math.round((Date.now() - this.booted) / 1000),
        throttle: { now: [], since_boot: busy ? [] : ["under-voltage"] }, age_s: j(0.9, 0.8),
      },
      obd: busy
        ? { iface: "can0", present: true, up: true, controller: "ERROR-ACTIVE", bitrate: 500000, tx_errors: 0,
            rx_errors: 0, rx_per_s: j(101, 3), last_frame_s: 0, verdict: "traffic" }
        : { iface: "can0", present: true, up: true, controller: "ERROR-ACTIVE", bitrate: 500000, tx_errors: 0,
            rx_errors: 0, rx_per_s: 0, last_frame_s: null, verdict: "quiet" },
    };
  }

  state() {
    const presets = this.builtin.map((p) => ({ ...p, builtin: true }))
      .concat([...this.user].sort((a, b) => a.name.localeCompare(b.name)).map((p) => ({ ...p, builtin: false })));
    return { ...this.live(), presets, active: this.active, tag: this.tag, supported: this.supported };
  }

  async request(cmd, args = {}) {
    await new Promise((r) => setTimeout(r, 150));
    const fail = (error) => { throw new Error(error); };
    switch (cmd) {
      case "hello": return { ok: true, protocol: 1, catalog: this.catalog, max_weight: 20, ...this.state() };
      case "state": return { ok: true, ...this.state() };
      case "status": return { ok: true, ...this.live() };
      case "save": {
        const p = args.preset;
        if (!NAME_OK.test((p.name || "").trim())) fail("name: 1-32 letters, digits, spaces or . ( ) & + - _");
        const weights = Object.fromEntries(Object.entries(p.weights).filter(([, v]) => v > 0));
        if (!Object.keys(weights).length) fail("turn on at least one value");
        const id = p.id || slugify(p.name);
        if (this.builtin.some((b) => b.id === id)) fail(`'${p.name}' is built in; save it under another name`);
        this.user = this.user.filter((u) => u.id !== id).concat({ id, name: p.name.trim(), weights });
        if (args.activate) this.activate(id);
        return { ok: true, saved: id, ...this.state() };
      }
      case "delete":
        if (this.builtin.some((b) => b.id === args.id)) fail("built-in presets can't be deleted");
        if (args.id === this.active) fail("that preset is active; apply another one first");
        this.user = this.user.filter((u) => u.id !== args.id);
        return { ok: true, ...this.state() };
      case "activate":
        if (!this.find(args.id)) fail(`no preset '${args.id}'`);
        this.activate(args.id);
        return { ok: true, ...this.state() };
      case "tag":
        if (args.tag && !TAG_OK.test(args.tag)) fail("tag: one word, letters, digits, . _ - (max 32)");
        this.tag = args.tag;
        return { ok: true, ...this.state() };
      default: fail(`unknown command '${cmd}'`);
    }
  }

  activate(id) {
    if (id !== this.active) this.started = Date.now();     // the logger starts a new file
    this.active = id;
  }
}

function slugify(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "preset";
}

// ---------- rates ----------

// Rows per second and per-value reads per second for a set of weights. The logger can make about
// reqPerS requests a second; a row costs one request per value due on it; rows are capped at hz.
function estimate(weights, reqPerS, hz) {
  const ws = Object.values(weights).filter((w) => w > 0);
  if (!ws.length) return { rows: 0, perRow: 0, rate: () => 0 };
  const top = Math.max(...ws);
  const perRow = ws.reduce((a, w) => a + w / top, 0);
  const rows = Math.min(hz, reqPerS / perRow);
  return { rows, perRow, rate: (w) => (w > 0 ? rows * w / top : 0) };
}

function fmtRate(r) {
  if (!r) return "off";
  if (r >= 1) return `${r >= 9.95 ? r.toFixed(0) : r.toFixed(1)}/s`;
  return `every ${(1 / r).toFixed(1)} s`;
}

function fmtDuration(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = h ? String(m).padStart(2, "0") : m;
  return `${h ? h + ":" : ""}${mm}:${String(sec).padStart(2, "0")}`;
}

// ---------- app ----------

const app = {
  link: null, catalog: [], maxWeight: 20, presets: [], active: null, tag: "", supported: null,
  status: { state: "unknown" }, open: null, editing: null, dirty: false, timer: null,
  system: null, obd: null, lastReply: 0, rtt: null, tab: store("carpi.tab") === "home" ? "home" : "dash",
};

function speed() {
  const s = app.status || {};
  const reqPerS = s.req_per_s || store("carpi.req_per_s") || DEFAULT_REQ_PER_S;
  const hz = s.hz || store("carpi.hz") || DEFAULT_HZ;
  return { reqPerS, hz };
}

function applyState(msg) {
  if (msg.catalog) app.catalog = msg.catalog;
  if (msg.max_weight) app.maxWeight = msg.max_weight;
  if (msg.presets) app.presets = msg.presets;
  if ("active" in msg) app.active = msg.active;
  if ("tag" in msg) app.tag = msg.tag;
  if ("supported" in msg) app.supported = msg.supported;
  app.lastReply = performance.now();
  if ("system" in msg) app.system = msg.system;
  if ("obd" in msg) app.obd = msg.obd;
  if (msg.status) {
    app.status = msg.status;
    if (msg.status.req_per_s) store("carpi.req_per_s", msg.status.req_per_s);
    if (msg.status.hz) store("carpi.hz", msg.status.hz);
  }
  // A status poll redraws only the status card: rebuilding the list mid-tap would eat the tap.
  if (msg.presets) render(); else { renderConn(false); renderStatus(); renderDash(); }
}

function toast(text, error = false) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.toggle("error", error);
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { el.hidden = true; }, error ? 5000 : 2500);
}

function confirmBox(text, yes = "OK") {
  const dlg = $("#confirm");
  $("#confirm-text").textContent = text;
  $("#confirm-yes").textContent = yes;
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "yes"), { once: true });
    dlg.returnValue = "";
    dlg.showModal();
  });
}

async function call(cmd, args) {
  try {
    const msg = await app.link.request(cmd, args);
    applyState(msg);
    return msg;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

function logging() { return app.status && app.status.state === "logging"; }

async function confirmApply(name) {
  if (!logging()) return true;
  return confirmBox(`A drive is being logged. Applying "${name}" closes the current file and starts a new one now.`, "Apply");
}

// ---------- connection ----------

async function connect(demo = false) {
  if (app.link && app.link.connected) { app.link.disconnect(); return; }
  if (!app.link || demo) {
    app.link = demo ? new DemoLink() : new BleLink();
    app.link.onclose = () => { stopPolling(); renderConn(); };
  }
  app.rtt = null;
  app.lastReply = 0;                     // nothing from this link yet
  $("#demo-note").hidden = !(app.link instanceof DemoLink);
  setPill("busy", "Connecting…");
  try {
    await app.link.connect();
    const t0 = performance.now();
    const hello = await app.link.request("hello");
    app.rtt = performance.now() - t0;
    applyState(hello);
  } catch (e) {
    if (e.name !== "NotFoundError") toast(friendly(e), true);   // NotFoundError = chooser cancelled
  }
  // Still linked even if that first request got no reply (it happens on the first connection
  // after pairing): keep polling, and the poller retries "hello" until one gets through.
  if (app.link.connected) startPolling();
  renderConn();
  renderDash();
}

function friendly(e) {
  if (e.name === "SecurityError") return "Bluetooth needs this page over HTTPS.";
  if (e.name === "NetworkError") return "Couldn't reach CarPi. Is the car on and the Pi booted?";
  if (/auth|encrypt|pair/i.test(e.message)) return "Pairing needed: run carpi_ble.py --pair on the Pi, then connect again.";
  return e.message;
}

function startPolling() {
  stopPolling();
  let inFlight = false;
  app.timer = setInterval(async () => {
    if (document.hidden || !app.link.connected || app.editing || inFlight) return;
    inFlight = true;                     // a slow reply must not stack up requests behind it
    const t0 = performance.now();
    try {
      const msg = await app.link.request(app.catalog.length ? "status" : "hello");
      app.rtt = performance.now() - t0;
      applyState(msg);
    } catch { /* next tick */ } finally { inFlight = false; }
  }, STATUS_EVERY_MS);
}

function stopPolling() { clearInterval(app.timer); app.timer = null; }

function setPill(kind, text) {
  const pill = $("#conn-pill");
  pill.classList.toggle("on", kind === "on");
  pill.classList.toggle("busy", kind === "busy");
  $("#conn-text").textContent = text;
}

// ---------- rendering ----------

function render() {
  renderConn();
  renderStatus();
  renderPresets();
  renderDash();
}

function renderConn(withList = true) {
  const on = !!(app.link && app.link.connected);
  setPill(on ? "on" : "off", on ? app.link.name : (app.link ? "Disconnected" : "Not connected"));
  $("#connect").textContent = on ? "Disconnect" : (app.link ? "Reconnect" : "Connect");
  $("#connect").classList.toggle("primary", !on);
  for (const el of ["#tag-input", "#tag-save", "#new-preset"]) $(el).disabled = !on;
  if (!on && withList) renderPresets();
  if (!on) renderDash();
}

function presetName(id) {
  const p = app.presets.find((x) => x.id === id);
  return p ? p.name : id;
}

function renderStatus() {
  const s = app.status || {};
  const line = $("#state-line"), sub = $("#state-sub");
  if (!app.link || !app.link.connected) {
    line.textContent = app.link ? "Not connected" : "Connect to see what CarPi is doing.";
    sub.textContent = "";
  } else if (s.state === "logging") {
    line.textContent = `Logging · ${fmtDuration(s.elapsed_s || 0)} · ${(s.rows || 0).toLocaleString()} rows`;
    const parts = [s.preset ? s.preset.name : presetName(app.active)];
    if (s.rows_per_s) parts.push(`${s.rows_per_s.toFixed(1)} rows/s`);
    if (s.drive) parts.push(s.drive);
    sub.textContent = parts.join(" · ");
  } else {
    line.textContent = STATES[s.state] || STATES.unknown;
    sub.textContent = app.active ? `Next drive: ${presetName(app.active)}` : "";
  }
  const input = $("#tag-input");
  if (document.activeElement !== input) input.value = app.tag || "";
}

function presetMeta(p) {
  const n = Object.keys(p.weights).length;
  const { reqPerS, hz } = speed();
  const est = estimate(usable(p.weights), reqPerS, hz);
  return `${n} value${n === 1 ? "" : "s"} · ≈ ${est.rows.toFixed(1)} rows/s`;
}

function usable(weights) {
  if (!app.supported) return weights;
  return Object.fromEntries(Object.entries(weights).filter(([id]) => app.supported.includes(id)));
}

function renderPresets() {
  const on = !!(app.link && app.link.connected);
  const ul = $("#presets");
  if (!app.presets.length) {
    ul.innerHTML = `<li class="sub">${on ? "No presets." : "Connect to load presets from CarPi."}</li>`;
    return;
  }
  ul.innerHTML = app.presets.map((p) => {
    const active = p.id === app.active;
    const open = p.id === app.open;
    const badges = (active ? `<span class="badge active">Active</span>` : "") + (p.builtin ? `<span class="badge">Built-in</span>` : "");
    const actions = open ? `<div class="actions">
        <button class="btn primary small" data-act="apply" ${active || !on ? "disabled" : ""}>Apply</button>
        ${p.builtin ? "" : `<button class="btn small" data-act="edit" ${on ? "" : "disabled"}>Edit</button>`}
        <button class="btn small" data-act="copy" ${on ? "" : "disabled"}>Copy</button>
        ${p.builtin ? "" : `<button class="btn small danger" data-act="delete" ${active || !on ? "disabled" : ""}>Delete</button>`}
      </div>` : "";
    return `<li class="preset${active ? " active" : ""}" data-id="${esc(p.id)}">
      <button class="head" type="button" aria-expanded="${open}">
        <span><span class="name">${esc(p.name)}</span><br><span class="meta">${presetMeta(p)}</span></span>
        <span class="badges">${badges}</span>
      </button>${actions}</li>`;
  }).join("");
}

// ---------- preset actions ----------

$("#presets").addEventListener("click", async (e) => {
  const li = e.target.closest(".preset");
  if (!li) return;
  const p = app.presets.find((x) => x.id === li.dataset.id);
  const act = e.target.closest("[data-act]");
  if (!act) {
    app.open = app.open === p.id ? null : p.id;
    renderPresets();
    return;
  }
  switch (act.dataset.act) {
    case "apply":
      if (await confirmApply(p.name) && await call("activate", { id: p.id })) toast(`Applied "${p.name}"`);
      break;
    case "edit": openEditor(p, false); break;
    case "copy": openEditor(p, true); break;
    case "delete":
      if (await confirmBox(`Delete "${p.name}"? Drives already logged with it are kept.`, "Delete")
          && await call("delete", { id: p.id })) toast(`Deleted "${p.name}"`);
      break;
  }
});

$("#tag-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tag = $("#tag-input").value.trim();
  if (tag && !TAG_OK.test(tag)) { toast("Tag: one word, letters, digits, . _ -", true); return; }
  if (await call("tag", { tag })) toast(tag ? `New drives tagged "${tag}"` : "Tag cleared");
});

$("#new-preset").addEventListener("click", () => openEditor(null, false));
$("#connect").addEventListener("click", () => connect(app.link instanceof DemoLink));
$("#demo-btn").addEventListener("click", () => { $("#no-bt").hidden = true; connect(true); });

// ---------- editor ----------

function openEditor(preset, copy) {
  const weights = preset ? { ...preset.weights } : {};
  let name = preset ? preset.name : "";
  if (copy) name = `Copy of ${name}`.slice(0, 32);
  app.editing = { id: preset && !copy ? preset.id : null, weights, original: name };
  app.dirty = false;
  $("#editor-title").textContent = app.editing.id ? "Edit preset" : "New preset";
  $("#preset-name").value = name;
  buildValues();
  $("#home").hidden = true;
  $(".tabs").hidden = true;
  $("#editor").hidden = false;
  window.scrollTo(0, 0);
}

async function closeEditor(force = false) {
  if (!force && app.dirty && !await confirmBox("Discard your changes to this preset?", "Discard")) return;
  app.editing = null;
  $("#editor").hidden = true;
  $(".tabs").hidden = false;
  $("#home").hidden = false;
  render();
}

function buildValues() {
  const groups = [];
  for (const c of app.catalog) {
    let g = groups.find((x) => x.name === c.group);
    if (!g) groups.push(g = { name: c.group, items: [] });
    g.items.push(c);
  }
  $("#values").innerHTML = groups.map((g) => `<div class="group"><h3>${esc(g.name)}</h3>${g.items.map((c) => {
    const na = app.supported && !app.supported.includes(c.id);
    const w = app.editing.weights[c.id] || 0;
    return `<div class="value${na ? " na" : ""}${w ? "" : " off"}" data-id="${c.id}">
      <div class="top-line">
        <span><span class="label">${esc(c.label)}</span><span class="unit">${esc(c.unit)}</span></span>
        <span class="rate"></span>
      </div>
      ${na ? `<div class="na-note">Not reported by this car</div>` : ""}
      <div class="slider">
        <input type="range" min="0" max="${app.maxWeight}" step="1" value="${w}" aria-label="${esc(c.label)} weight">
        <output>${w}</output>
      </div></div>`;
  }).join("")}</div>`).join("");
  updateRates();
}

function updateRates() {
  const { reqPerS, hz } = speed();
  const w = app.editing.weights;
  const est = estimate(usable(w), reqPerS, hz);
  for (const row of document.querySelectorAll("#values .value")) {
    const id = row.dataset.id;
    const na = app.supported && !app.supported.includes(id);
    row.querySelector(".rate").textContent = na && w[id] ? "skipped" : fmtRate(est.rate(na ? 0 : w[id] || 0));
    row.classList.toggle("off", !w[id]);
  }
  const on = Object.keys(w).filter((id) => w[id] > 0);
  const missing = app.catalog.filter((c) => c.viewer && !w[c.id]).map((c) => c.label.toLowerCase());
  $("#summary").innerHTML = on.length
    ? `<strong>${on.length} value${on.length === 1 ? "" : "s"}</strong> · ≈ ${est.rows.toFixed(1)} rows/s · ${est.perRow.toFixed(1)} requests per row`
      + (missing.length ? `<span class="warn">Without ${esc(missing.join(", "))}, these drives won't open in the public viewer (the local analyzer is fine).</span>` : "")
    : `<strong>Nothing selected.</strong> Turn on at least one value.`;
}

$("#values").addEventListener("input", (e) => {
  if (e.target.type !== "range") return;
  const row = e.target.closest(".value");
  const w = Number(e.target.value);
  if (w) app.editing.weights[row.dataset.id] = w; else delete app.editing.weights[row.dataset.id];
  row.querySelector("output").textContent = w;
  app.dirty = true;
  updateRates();
});

$("#preset-name").addEventListener("input", () => { app.dirty = true; });

document.querySelector(".quick").addEventListener("click", (e) => {
  const fill = e.target.dataset.fill;
  if (fill === undefined) return;
  const def = app.presets.find((p) => p.id === "default");
  app.editing.weights = fill === "default" && def ? { ...def.weights } : {};
  app.dirty = true;
  buildValues();
});

$("#editor-close").addEventListener("click", () => closeEditor());

async function save(activate) {
  const name = $("#preset-name").value.trim();
  if (!NAME_OK.test(name)) { toast("Name: 1-32 letters, digits, spaces or . ( ) & + - _", true); $("#preset-name").focus(); return; }
  const weights = Object.fromEntries(Object.entries(app.editing.weights).filter(([, w]) => w > 0));
  if (!Object.keys(weights).length) { toast("Turn on at least one value", true); return; }
  const id = app.editing.id || slugify(name);
  if (!app.editing.id && app.presets.some((p) => p.id === id)) {
    const other = app.presets.find((p) => p.id === id);
    if (other.builtin) { toast(`"${other.name}" is built in; pick another name`, true); return; }
    if (!await confirmBox(`Replace the saved preset "${other.name}"?`, "Replace")) return;
  }
  const willApply = activate || id === app.active;     // saving the active preset applies it too
  if (willApply && !await confirmApply(name)) return;
  const msg = await call("save", { preset: { id: app.editing.id || undefined, name, weights }, activate });
  if (!msg) return;
  toast(willApply ? `Saved and applied "${name}"` : `Saved "${name}"`);
  app.open = msg.saved;
  closeEditor(true);
}

$("#save").addEventListener("click", () => save(false));
$("#save-apply").addEventListener("click", () => save(true));

// ---------- dashboard ----------

function showTab(tab) {
  app.tab = tab;
  store("carpi.tab", tab);
  $("#dash").hidden = tab !== "dash";
  $("#home").hidden = tab !== "home";
  $("#tab-dash").setAttribute("aria-selected", String(tab === "dash"));
  $("#tab-home").setAttribute("aria-selected", String(tab === "home"));
  $("#page-title").textContent = tab === "dash" ? "Dashboard" : "Logging";
  if (tab === "dash") renderDash();
}

function chip(el, kind, text, live = false) {
  el.className = `chip${kind ? " " + kind : ""}${live ? " live" : ""}`;
  el.textContent = text;
}

const num = (v) => typeof v === "number" && Number.isFinite(v);
const fix = (v, d = 0) => (num(v) ? v.toFixed(d) : "—");

function ago(s) {
  if (!num(s)) return "never";
  if (s < 1.5) return "just now";
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

function fmtUptime(s) {
  if (!num(s)) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function connected() { return !!(app.link && app.link.connected); }

function renderLink() {
  const on = connected();
  const card = $("#link-card");
  card.classList.toggle("is-off", !on);
  $("#dash-hint").hidden = on;
  if (!on) {
    chip($("#link-chip"), app.link ? "crit" : "", app.link ? "Lost" : "Offline");
    $("#link-line").textContent = app.link ? "Disconnected" : "Not connected";
    $("#link-sub").textContent = app.link ? "Tap Reconnect. The Pi may have lost power (ignition off)." : "Tap Connect and pick CarPi.";
    return;
  }
  const since = app.lastReply ? (performance.now() - app.lastReply) / 1000 : null;
  const stale = since === null || since * 1000 > STALE_MS;
  chip($("#link-chip"), stale ? "warn" : "ok", stale ? "No reply" : "Live", !stale);
  setPill(stale ? "busy" : "on", stale ? "No reply" : app.link.name);
  $("#link-line").textContent = stale ? "Connected, not responding" : `Connected to ${app.link.name}`;
  const parts = [`last reply ${ago(since)}`];
  if (num(app.rtt)) parts.push(`round trip ${Math.round(app.rtt)} ms`);
  parts.push(`updates every ${STATUS_EVERY_MS / 1000} s`);
  $("#link-sub").textContent = parts.join(" · ");
}

// obd.verdict (carpi_system.py) + the logger's state -> [chip kind, chip text, headline, detail]
function carSummary() {
  const o = app.obd, s = app.status || {};
  const engine = {
    logging: "Engine running, logging",
    detecting: "Car awake, looking for the engine",
    ignition_off: "Car awake, engine off",
    waiting: "Car awake, logger waiting for the engine",
    offline: "Logger isn't running",
  }[s.state] || "Logger status unknown";
  if (!o) {      // a Pi running the older carpi_ble.py: only the logger's view
    return s.state === "logging" ? ["ok", "Plugged in", "Engine running, logging", "Update carpi_ble.py on the Pi for OBD link details."]
      : ["", "Unknown", STATES[s.state] || STATES.unknown, "Update carpi_ble.py on the Pi for OBD link details."];
  }
  switch (o.verdict) {
    case "traffic": return ["ok", "Plugged in", engine, "CAN traffic from the car's gateway: the OBD-II cable is connected."];
    case "quiet": return ["warn", "No traffic", "Not seeing the car", "No CAN frames: the OBD-II cable is unplugged, or the car is asleep."];
    case "checking": return ["", "Checking", "Checking the OBD-II link…", "Needs two readings, about 2 seconds."];
    case "down": return ["crit", "can0 down", "CAN interface is down", "can0.service didn't bring it up: sudo systemctl restart can0"];
    case "bus_off": return ["crit", "Bus-off", "CAN bus error (bus-off)", "The controller gave up after too many errors. Check CAN-H / CAN-L and the 500 kbps bitrate."];
    case "no_interface": return ["crit", "No CAN HAT", "CAN HAT not found", "can0 doesn't exist: check the HAT and the mcp2515 overlay in config.txt."];
    default: return ["", "Unknown", "OBD-II link unknown", o.error || ""];
  }
}

function renderCar() {
  const on = connected();
  $("#car-card").classList.toggle("is-off", !on);
  if (!on || !app.lastReply) {
    chip($("#car-chip"), "", "—");
    $("#car-line").textContent = "—";
    $("#car-sub").textContent = on ? "Waiting for the first reading." : "Connect to see the OBD-II link and the logger.";
    $("#car-kv").innerHTML = "";
    return;
  }
  const [kind, label, line, sub] = carSummary();
  const s = app.status || {}, o = app.obd || {};
  chip($("#car-chip"), kind, label, kind === "ok");
  $("#car-line").textContent = line;
  $("#car-sub").textContent = sub;
  const rows = [];
  if (app.obd) {
    rows.push(["CAN", [o.iface, o.controller ? o.controller.toLowerCase() : null, num(o.bitrate) ? `${o.bitrate / 1000} kbps` : null].filter(Boolean).join(" · ") || "—"]);
    const last = num(o.last_frame_s) ? `last ${ago(o.last_frame_s)}` : "none since the Pi started";
    rows.push(["Frames", num(o.rx_per_s) && o.rx_per_s > 0 ? `${o.rx_per_s.toFixed(o.rx_per_s < 10 ? 1 : 0)}/s · ${last}` : last]);
    if ((o.tx_errors || 0) + (o.rx_errors || 0) > 0) rows.push(["Errors", `tx ${o.tx_errors} · rx ${o.rx_errors}`]);
  }
  if (s.state === "logging") {
    rows.push(["Drive", `${fmtDuration(s.elapsed_s || 0)} · ${(s.rows || 0).toLocaleString()} rows${num(s.rows_per_s) ? ` · ${s.rows_per_s.toFixed(1)}/s` : ""}`]);
    if (s.drive) rows.push(["File", s.drive]);
  }
  rows.push(["Preset", s.preset ? s.preset.name : (app.active ? presetName(app.active) : "—")]);
  rows.push(["Tag", app.tag || "none"]);
  $("#car-kv").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

// [label, value html, unit, sub text, meter 0..1 or null, level]
function piTiles(y) {
  const level = (v, warn, crit) => (!num(v) ? "" : v >= crit ? "crit" : v >= warn ? "warn" : "");
  return [
    ["CPU temp", fix(y.cpu_temp_c, 1), "°C", "throttles at 80 °C", num(y.cpu_temp_c) ? y.cpu_temp_c / 85 : null, level(y.cpu_temp_c, 70, 80)],
    ["CPU load", fix(y.cpu_pct, 0), "%", num(y.load_1m) ? `avg ${y.load_1m.toFixed(2)}${y.cores ? ` · ${y.cores} cores` : ""}` : "", num(y.cpu_pct) ? y.cpu_pct / 100 : null, level(y.cpu_pct, 75, 95)],
    ["Clock", fix(y.cpu_mhz, 0), "MHz", num(y.cpu_max_mhz) ? `max ${y.cpu_max_mhz.toFixed(0)} MHz` : "", num(y.cpu_mhz) && num(y.cpu_max_mhz) ? y.cpu_mhz / y.cpu_max_mhz : null, ""],
    ["Memory", fix(y.mem_pct, 0), "%", num(y.mem_total_mb) ? `of ${(y.mem_total_mb / 1024).toFixed(1)} GB` : "", num(y.mem_pct) ? y.mem_pct / 100 : null, level(y.mem_pct, 80, 92)],
    ["SD card", fix(y.disk_free_gb, 1), "GB free", num(y.disk_pct) ? `${y.disk_pct.toFixed(0)}% used` : "", num(y.disk_pct) ? y.disk_pct / 100 : null, level(y.disk_pct, 85, 95)],
    ["Uptime", esc(fmtUptime(y.uptime_s)), "", "since power-on", null, ""],
  ];
}

function renderPi() {
  const on = connected() && app.system && !app.system.error;
  $("#pi-card").classList.toggle("is-off", !on);
  const y = on ? app.system : {};
  $("#pi-tiles").innerHTML = piTiles(y).map(([k, v, unit, sub, meter, lvl]) => `
    <div class="tile${lvl ? " " + lvl : ""}">
      <div class="k">${esc(k)}</div>
      <div class="v">${v}${unit ? `<small>${esc(unit)}</small>` : ""}</div>
      ${meter === null ? "" : `<div class="meter" aria-hidden="true"><i style="width:${Math.max(0, Math.min(1, meter)) * 100}%"></i></div>`}
      <div class="s">${esc(sub)}</div>
    </div>`).join("");
  const power = $("#pi-power");
  const t = y.throttle;
  let kind = "", text = "";
  if (!connected()) text = "";
  else if (app.system && app.system.error) { kind = "warn"; text = `Pi readings failed: ${app.system.error}`; }
  else if (!app.system) text = "Update carpi_ble.py on the Pi to see its readings.";
  else if (!t) text = "Power and throttling flags aren't available on this Pi.";
  else if (t.now.length) { kind = "crit"; text = `Now: ${t.now.join(", ")}. The car's USB port may not supply 5 V / 3 A.`; }
  else if (t.since_boot.length) { kind = "warn"; text = `Since boot: ${t.since_boot.join(", ")}. Fine right now.`; }
  else { kind = "ok"; text = "Power OK: no under-voltage or throttling since boot."; }
  power.className = `power${kind ? " " + kind : ""}`;
  power.textContent = text;
  const lvls = piTiles(y).map((x) => x[5]);
  const worst = (t && t.now.length) || lvls.includes("crit") ? "crit" : lvls.includes("warn") || (t && t.since_boot.length) ? "warn" : "ok";
  chip($("#pi-chip"), on ? worst : "", on ? { ok: "Healthy", warn: "Check", crit: "Problem" }[worst] : "—");
}

function renderDash() {
  if (app.tab !== "dash") return;
  renderLink();
  renderCar();
  renderPi();
}

// ---------- start ----------

$(".tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) showTab(tab.id === "tab-home" ? "home" : "dash");
});
setInterval(() => { if (!document.hidden) renderLink(); }, 1000);   // "last reply 3 s ago"
showTab(app.tab);
if (DEBUG) {
  $("#debug-card").hidden = false;
  $("#debug-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(debugLines.join("\n")); toast("Debug log copied"); }
    catch { toast("Couldn't copy: select the text instead", true); }
  });
  dbg(`page loaded: ${navigator.userAgent}`);
}
if (!("bluetooth" in navigator)) $("#no-bt").hidden = false;
if (new URLSearchParams(location.search).has("demo")) { $("#no-bt").hidden = true; connect(true); }
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && app.link && app.link.connected && !app.editing) call("status");
});
if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
render();
