"""
carpi_presets.py - which values CarPi logs, and how often.

A preset gives each value in CATALOG a weight from 0 (off) to MAX_WEIGHT. Values with the
highest weight in the preset are read on every CSV row; a value with half that weight is read
every other row, and so on. The built-in "default" preset (12 for the old fast PIDs, 1 for the
old slow ones) reproduces the original fast/slow polling exactly.

Files (all under ~/carpi/ on the Pi):
  presets/<id>.json     presets saved from the phone: {"id", "name", "weights": {"0C": 12, ...}}
  active_preset.json    a full copy of the preset the logger uses; read at each drive start
                        and checked every second while logging
  supported.json        PIDs the car answered to on the last drive (written by the logger)
  tag.txt               one word copied into each drive's metadata

Only values in CATALOG can be selected, and every one of them is a standard read-only OBD-II
Mode 01 PID the logger already knows how to decode. Nothing here can make the logger send a new
kind of request.

No dependencies beyond the standard library: imported by carpi_logger.py and carpi_ble.py.
"""
import json
import os
import re
from pathlib import Path

MAX_WEIGHT = 20
MAX_PRESETS = 30

# id: (label, unit, group, columns). Order = CSV column order. The first 12 were the old FAST
# list and the last 12 the old SLOW list. VW-specific values (UDS 0x22 DIDs) get added here only
# once they're confirmed on the car and decoded in carpi_logger.py.
CATALOG = {
    "0C": ("Engine speed", "rpm", "Engine", ["rpm"]),
    "0B": ("Manifold pressure", "kPa", "Air & boost", ["map_kpa"]),
    "70": ("Boost pressure", "kPa", "Air & boost", ["boost_cmd_kpa", "boost_act_kpa"]),
    "04": ("Calculated load", "%", "Engine", ["load_pct"]),
    "0E": ("Ignition timing", "°", "Engine", ["timing_deg"]),
    "06": ("Short-term fuel trim", "%", "Fueling", ["stft_pct"]),
    "44": ("Commanded lambda", "λ", "Fueling", ["lambda_cmd"]),
    "34": ("Lambda (wideband)", "λ", "Fueling", ["lambda", "o2s1_ma"]),
    "23": ("Fuel rail pressure", "kPa", "Fueling", ["rail_kpa"]),
    "49": ("Accelerator pedal", "%", "Driver", ["pedal_pct"]),
    "11": ("Throttle", "%", "Driver", ["throttle_pct"]),
    "0D": ("Vehicle speed", "km/h", "Driver", ["speed_kph"]),
    "05": ("Coolant temperature", "°C", "Temperatures", ["coolant_c"]),
    "0F": ("Intake air temperature", "°C", "Temperatures", ["iat_c"]),
    "07": ("Long-term fuel trim", "%", "Fueling", ["ltft_pct"]),
    "42": ("Module voltage", "V", "Other", ["volts"]),
    "33": ("Barometric pressure", "kPa", "Air & boost", ["baro_kpa"]),
    "46": ("Ambient temperature", "°C", "Temperatures", ["ambient_c"]),
    "2F": ("Fuel level", "%", "Other", ["fuel_pct"]),
    "3C": ("Catalyst temperature", "°C", "Temperatures", ["cat_temp_c"]),
    "15": ("Rear O2 sensor", "V", "Fueling", ["o2s2_v", "o2s2_trim_pct"]),
    "56": ("Long-term rear O2 trim", "%", "Fueling", ["lt_o2s2_trim_pct"]),
    "43": ("Absolute load", "%", "Engine", ["abs_load_pct"]),
    "1F": ("Engine run time", "s", "Other", ["run_time_s"]),
}
# PIDs behind the columns the GitHub Pages viewer requires (analysis/carpi_analysis.py REQUIRED)
VIEWER_PIDS = {"0C", "0D", "06", "07", "05", "44", "70", "33"}

_OLD_FAST = list(CATALOG)[:12]

BUILTIN = {
    "default": {"id": "default", "name": "Full (default)",
                "weights": {k: 12 if k in _OLD_FAST else 1 for k in CATALOG}},
    # Idle trim study (PCV before/after): trims, air and lambda as often as possible.
    "fuel-trim": {"id": "fuel-trim", "name": "Fuel trim study", "weights": {
        "0C": 12, "0B": 12, "70": 4, "04": 6, "06": 12, "44": 12, "34": 12, "11": 4, "0D": 4,
        "05": 1, "0F": 1, "07": 6, "33": 1, "15": 1}},
    # Hard pulls: everything that moves in a second or two, temperatures barely.
    "pulls": {"id": "pulls", "name": "Pulls (boost & timing)", "weights": {
        "0C": 12, "0B": 6, "70": 12, "04": 12, "0E": 12, "06": 3, "44": 12, "34": 12, "23": 12,
        "49": 12, "11": 12, "0D": 6, "05": 1, "0F": 1, "07": 1, "33": 1}},
}

_NAME_OK = re.compile(r"^[\w .()&+-]{1,32}$")
_TAG_OK = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


class PresetError(ValueError):
    """A preset, name or tag that can't be accepted."""


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:32] or "preset"


def validate(obj):
    """Return a clean copy of a preset dict, or raise PresetError. Unknown PIDs are an error, not
    silently dropped, so the phone hears about a typo instead of logging less than expected."""
    if not isinstance(obj, dict):
        raise PresetError("preset must be an object")
    name = obj.get("name")
    if not isinstance(name, str) or not _NAME_OK.match(name.strip()):
        raise PresetError("name: 1-32 letters, digits, spaces or . ( ) & + - _")
    name = name.strip()
    raw = obj.get("weights")
    if not isinstance(raw, dict):
        raise PresetError("weights must be an object")
    weights = {}
    for pid, w in raw.items():
        if pid not in CATALOG:
            raise PresetError(f"unknown value {pid!r}")
        if isinstance(w, bool) or not isinstance(w, int) or not 0 <= w <= MAX_WEIGHT:
            raise PresetError(f"{pid}: weight must be a whole number 0-{MAX_WEIGHT}")
        if w:
            weights[pid] = w
    if not weights:
        raise PresetError("turn on at least one value")
    pid_order = list(CATALOG)
    weights = dict(sorted(weights.items(), key=lambda kv: pid_order.index(kv[0])))
    pid = obj.get("id") or slugify(name)
    if not isinstance(pid, str) or slugify(pid) != pid:
        raise PresetError("bad id")
    return {"id": pid, "name": name, "weights": weights}


BUILTIN = {k: validate(v) for k, v in BUILTIN.items()}   # same shape and order as saved ones
DEFAULT = BUILTIN["default"]


def check_tag(tag):
    tag = (tag or "").strip()
    if tag and not _TAG_OK.match(tag):
        raise PresetError("tag: one word, letters, digits, . _ - (max 32)")
    return tag


def atomic_write(path, text):
    """Write-then-rename so a power cut never leaves a half-written file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_active(path):
    """The preset in active_preset.json; DEFAULT if there's no file; None if it's unreadable or
    invalid (the logger then keeps whatever it's using rather than switching on a bad file)."""
    path = Path(path)
    if not path.exists():
        return DEFAULT
    try:
        return validate(read_json(path))
    except PresetError:
        return None


class Schedule:
    """Which PIDs to request on each row, from integer weights.

    Each PID earns its weight in credit every row and is read whenever it has a full row's worth
    (the top weight). PIDs with the same weight start at evenly spread offsets, so twelve weight-1
    PIDs under a top weight of 12 take turns, one per row, like the old slow round-robin.
    """

    def __init__(self, weights):
        self.pids = list(weights)
        self.weights = dict(weights)
        self.top = max(self.weights.values())
        self.credit = {}
        groups = {}
        for pid in self.pids:
            groups.setdefault(self.weights[pid], []).append(pid)
        for w, members in groups.items():
            n = len(members)
            for j, pid in enumerate(members):
                offset_rows = (j * self.top) // (w * n)   # spread over one period (top/w rows)
                self.credit[pid] = self.top - w - offset_rows * w

    def next_row(self):
        row = []
        for pid in self.pids:
            self.credit[pid] += self.weights[pid]
            if self.credit[pid] >= self.top:
                self.credit[pid] -= self.top
                row.append(pid)
        return row


class Store:
    """Presets, the active preset, the tag and the supported-PID list in one folder (~/carpi)."""

    def __init__(self, root):
        self.root = Path(root).expanduser()
        self.preset_dir = self.root / "presets"
        self.active_path = self.root / "active_preset.json"
        self.tag_path = self.root / "tag.txt"
        self.supported_path = self.root / "supported.json"

    def user_presets(self):
        out = {}
        for p in sorted(self.preset_dir.glob("*.json")):
            try:
                preset = validate(read_json(p))
            except PresetError:
                continue
            if preset["id"] == p.stem and preset["id"] not in BUILTIN:
                out[preset["id"]] = preset
        return out

    def presets(self):
        """Built-ins first, then saved ones by name. Each has a "builtin" flag."""
        out = [dict(p, builtin=True) for p in BUILTIN.values()]
        user = sorted(self.user_presets().values(), key=lambda p: p["name"].lower())
        return out + [dict(p, builtin=False) for p in user]

    def get(self, pid):
        if pid in BUILTIN:
            return BUILTIN[pid]
        preset = self.user_presets().get(pid)
        if preset is None:
            raise PresetError(f"no preset {pid!r}")
        return preset

    def save(self, obj):
        preset = validate(obj)
        if preset["id"] in BUILTIN:
            raise PresetError(f"{preset['name']!r} is built in; save it under another name")
        existing = self.user_presets()
        if preset["id"] not in existing and len(existing) >= MAX_PRESETS:
            raise PresetError(f"at most {MAX_PRESETS} saved presets")
        self.preset_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.preset_dir / f"{preset['id']}.json", json.dumps(preset, indent=2))
        if self.active_id() == preset["id"]:
            self._write_active(preset)      # edits to the active preset apply right away
        return preset

    def delete(self, pid):
        if pid in BUILTIN:
            raise PresetError("built-in presets can't be deleted")
        if pid == self.active_id():
            raise PresetError("that preset is active; apply another one first")
        path = self.preset_dir / f"{pid}.json"
        if slugify(pid) != pid or not path.exists():
            raise PresetError(f"no preset {pid!r}")
        path.unlink()

    def activate(self, pid):
        preset = self.get(pid)
        self._write_active(preset)
        return preset

    def active(self):
        return read_active(self.active_path) or DEFAULT

    def active_id(self):
        return self.active()["id"]

    def _write_active(self, preset):
        self.root.mkdir(parents=True, exist_ok=True)
        clean = {k: preset[k] for k in ("id", "name", "weights")}
        atomic_write(self.active_path, json.dumps(clean, indent=2))

    def tag(self):
        try:
            words = self.tag_path.read_text(encoding="utf-8").split()
        except OSError:
            return ""
        return words[0] if words else ""

    def set_tag(self, tag):
        tag = check_tag(tag)
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write(self.tag_path, tag + "\n" if tag else "")
        return tag

    def supported(self):
        data = read_json(self.supported_path)
        pids = data.get("pids") if isinstance(data, dict) else None
        return [p for p in pids if p in CATALOG] if isinstance(pids, list) else None
