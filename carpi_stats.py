#!/usr/bin/env python3
"""
carpi_stats.py - drive analysis on the Pi, for the phone page's Analysis tab (protocol 6).

Standard library only. The definitions are the public viewer's (analysis/carpi_analysis.py) and
the PC analyzer's (carpi_app/filters.py, events.py, views/map3d.py), so the three agree; tests
compare them on the sample drive.

    d = Drive()
    d.feed(text)          # CSV text in any chunks, header first; a partial last line waits
    d.finish()            # the file is complete: a cut-off last line is dropped, every row settles
    d.summary()           # fuel trim by rpm band, idle rpm, peak boost, ...
    d.events()            # hard accelerations, timing dips, lean under boost, ...
    d.grid("rpm", "load_pct", "trim_total", ["running", "warm", "closed_loop"]).view("mean")

A row settles once a row more than SETTLE_S later has arrived: the DSG-shift test looks 0.4 s
ahead. Summary, events and maps take settled rows only and add each row once, so a drive being
recorded costs only its new rows, and nothing already shown changes afterwards.
"""
import math
import statistics
from array import array
from collections import OrderedDict

NAN = float("nan")
SETTLE_S = 0.5
SHIFT_PAD_S = 0.4
KPA_PER_PSI = 6.895
NOT_DATA = ("time", "elapsed_s", "clock_ok")
DERIVED = ("boost_psi", "trim_total", "lambda_err", "rail_bar")

# column -> (label, unit, map step): carpi_app/channels.py CHANNELS, then the derived values
COLUMNS = {
    "rpm": ("Engine speed", "rpm", 500),
    "speed_kph": ("Vehicle speed", "km/h", 10),
    "pedal_pct": ("Accelerator pedal", "%", 10),
    "throttle_pct": ("Throttle plate", "%", 10),
    "load_pct": ("Calculated load", "%", 10),
    "abs_load_pct": ("Absolute load", "%", 10),
    "map_kpa": ("Manifold pressure", "kPa", 20),
    "boost_act_kpa": ("Charge pressure (abs)", "kPa", 20),
    "boost_cmd_kpa": ("Charge pressure cmd (abs)", "kPa", 20),
    "timing_deg": ("Ignition timing", "°", 2),
    "lambda": ("Lambda actual", "", 0.02),
    "lambda_cmd": ("Lambda commanded", "", 0.02),
    "stft_pct": ("Short-term fuel trim", "%", 2),
    "ltft_pct": ("Long-term fuel trim", "%", 2),
    "o2s1_ma": ("Upstream O2 current", "mA", 0.1),
    "o2s2_v": ("Downstream O2", "V", 0.1),
    "o2s2_trim_pct": ("Downstream O2 trim", "%", 2),
    "lt_o2s2_trim_pct": ("Downstream O2 LT trim", "%", 2),
    "rail_kpa": ("Fuel rail pressure", "kPa", 2000),
    "coolant_c": ("Coolant temp", "°C", 5),
    "iat_c": ("Intake air temp", "°C", 5),
    "ambient_c": ("Ambient temp", "°C", 5),
    "cat_temp_c": ("Catalyst temp", "°C", 50),
    "volts": ("Module voltage", "V", 0.25),
    "baro_kpa": ("Barometric pressure", "kPa", 1),
    "fuel_pct": ("Fuel level", "%", 10),
    "run_time_s": ("Engine run time", "s", 60),
    "boost_psi": ("Boost (gauge)", "psi", 2),
    "trim_total": ("Total fuel trim", "%", 2),
    "lambda_err": ("Lambda error (act - cmd)", "", 0.01),
    "rail_bar": ("Fuel rail pressure", "bar", 20),
}

# Row filters (carpi_app/filters.py), one bit each. "no_shift" in a map's filters means NOT SHIFT.
RUNNING, WARM, CLOSED_LOOP, IDLE, MOVING, CRUISE, BOOST, HARD, SHIFT = (1 << i for i in range(9))
FILTERS = {"running": RUNNING, "warm": WARM, "closed_loop": CLOSED_LOOP, "idle": IDLE,
           "moving": MOVING, "cruise": CRUISE, "boost": BOOST, "hard": HARD, "no_shift": SHIFT}
STEADY = RUNNING | WARM | CLOSED_LOOP      # where fuel trims mean something (carpi_analysis._steady)
STATS = ("mean", "min", "max", "rows")
MAX_GRIDS = 6                              # maps kept per drive, newest last
MAX_BINS = 12                              # cells across and down on the phone
MIN_ROWS = 5                               # fewer rows in a cell: not shown (map3d's min_n)
HARD_PEDAL, HARD_THROTTLE = 45, 75         # this car's pedal reads ~14.5 % at rest, ~60 % floored
BANDS = [("Idle, under 1,000", 0, 1000), ("1,000–1,800", 1000, 1800),
         ("1,800–2,500", 1800, 2500), ("2,500 and up", 2500, math.inf)]   # carpi_analysis.BANDS


def _num(cell):
    """A CSV cell as a number; None when it's blank or garbled (the last value stands in)."""
    try:
        v = float(cell)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _ok(v):
    return v == v                          # False for NaN


def _v(d, col, k):
    a = d.data.get(col)
    return a[k] if a is not None else NAN


def step(col):
    """A column's map bin size (1 for columns the table doesn't know)."""
    return COLUMNS.get(col, (col, "", 1))[2]


class Drive:
    """One drive's rows, kept as columns, and its analysis so far."""

    def __init__(self):
        self.cols = None                   # the CSV header
        self.data = {}                     # column -> array("d"); blanks hold the last value
        self.flags = array("H")            # FILTERS bits, one per settled row
        self.n = 0                         # rows read
        self.settled = 0                   # rows counted into the summary, events and maps
        self.done = False
        self._partial = ""
        self._last = {}                    # column -> last real value (its keys: columns with data)
        self._dseen = set()                # derived columns that have had a value
        self._ie = 0
        self._slots = []                   # (cell index, column, its array), "time" left out
        self._core = array("b")            # rows where a DSG upshift cuts timing (shift_mask's core)
        self._sum = Summary()
        self._detectors = [Detector(*spec) for spec in DETECTORS]
        self._grids = OrderedDict()        # (x, y, z, filters) -> Grid, newest last

    # ---- reading ----

    def feed(self, text):
        """CSV text, in chunks of any size. NUL padding from a power cut is dropped."""
        text = self._partial + text.replace("\0", "")
        lines = text.split("\n")
        self._partial = lines.pop()                    # no newline yet: wait for the rest
        for line in lines:
            self._line(line.rstrip("\r"))
        self._settle()

    def finish(self):
        """The file is complete: a cut-off last line is dropped and every row settles."""
        self._partial = ""
        self.done = True
        self._settle()
        for det in self._detectors:
            det.close(self)

    def _line(self, line):
        if not line.strip():
            return
        cells = line.split(",")
        if self.cols is None:
            self.cols = [c.strip() for c in cells]
            if "elapsed_s" not in self.cols:
                raise ValueError("not a CarPi drive: no elapsed_s column")
            self._ie = self.cols.index("elapsed_s")
            for c in self.cols + list(DERIVED):
                if c != "time":
                    self.data.setdefault(c, array("d"))
            self._slots = [(i, c, self.data[c]) for i, c in enumerate(self.cols) if c != "time"]
            return
        if len(cells) > len(self.cols):
            return                                     # garbled line
        cells += [""] * (len(self.cols) - len(cells))
        if _num(cells[self._ie]) is None:
            return                                     # a row without its time can't be placed
        last = self._last
        for i, name, arr in self._slots:               # the hot loop: most cells are blank
            cell = cells[i]
            if cell:
                try:
                    v = float(cell)
                except ValueError:
                    v = NAN
                if v - v == 0:                         # a finite number
                    last[name] = v
                    arr.append(v)
                    continue
            arr.append(last.get(name, NAN))            # blank or garbled: the last value (ffill)

        def g(c):
            return last.get(c, NAN)
        data = self.data
        for name, v in (("boost_psi", (g("boost_act_kpa") - g("baro_kpa")) / KPA_PER_PSI),
                        ("trim_total", g("stft_pct") + g("ltft_pct")),
                        ("lambda_err", g("lambda") - g("lambda_cmd")),
                        ("rail_bar", g("rail_kpa") / 100)):
            data[name].append(v)
            if v == v:
                self._dseen.add(name)
        self.n += 1

    def columns(self):
        """Columns with data, for a map's axes: the known ones in COLUMNS order, then any others."""
        seen = set(self._last) | self._dseen
        known = [c for c in COLUMNS if c in seen]
        return known + sorted(c for c in seen if c not in COLUMNS and c not in NOT_DATA)

    def summary(self):
        """analysis/carpi_analysis.py summarize() + trim_bands(): see Summary."""
        return self._sum.out(self)

    def events(self):
        """carpi_app/events.py detectors, oldest first; a span still going on a live drive is
        included with "ongoing": true once it's long enough."""
        return sorted((e for det in self._detectors for e in det.events(self)),
                      key=lambda e: (e["t0"], e["kind"]))

    def grid(self, x, y, z, filters=()):
        """The map of z over x and y, for rows passing every filter (names from FILTERS). Kept up to
        date as rows settle; the newest MAX_GRIDS are kept."""
        for c in (x, y, z):
            if c not in self.data or c in NOT_DATA:
                raise ValueError(f"{c} isn't in this drive")
        for f in filters:
            if f not in FILTERS:
                raise ValueError(f"unknown filter {f!r}: use {', '.join(FILTERS)}")
        key = (x, y, z, tuple(sorted(set(filters))))
        g = self._grids.pop(key, None) or Grid(x, y, z, key[3])
        g.catch_up(self)
        self._grids[key] = g
        while len(self._grids) > MAX_GRIDS:
            self._grids.popitem(last=False)
        return g

    # ---- settling ----

    def _settle(self):
        t = self.data.get("elapsed_s")
        if not t:
            return
        n = self.n
        self._mark_core(n if self.done else n - 1)     # a core row needs the row after it
        horizon = math.inf if self.done else t[n - 1] - SETTLE_S
        start = self.settled
        while self.settled < n and t[self.settled] < horizon:
            k = self.settled
            self.flags.append(self._row_flags(k, t))
            self._count(k)
            self.settled += 1
        if self.settled > start:
            self._caught_up()

    def _mark_core(self, upto):
        """shift_mask()'s core: rpm drops > 300 in one row while timing is at -10° or below."""
        rpm, tim, n = self.data.get("rpm"), self.data.get("timing_deg"), self.n
        for j in range(len(self._core), upto):
            hit = False
            if rpm is not None and tim is not None:
                cut = tim[j] <= -10 or (j + 1 < n and tim[j + 1] <= -10)
                hit = cut and ((j > 0 and rpm[j] - rpm[j - 1] < -300)
                               or (j + 1 < n and rpm[j + 1] - rpm[j] < -300))
            self._core.append(hit)

    def _in_shift(self, k, t):
        """Within SHIFT_PAD_S of a core row (carpi_app/events.py shift_mask). Compared as
        t[core] - pad <= t <= t[core] + pad, the analyzer's arithmetic, so boundary rows agree."""
        core = self._core
        j = k
        while j >= 0 and t[k] <= t[j] + SHIFT_PAD_S:
            if j < len(core) and core[j]:
                return True
            j -= 1
        j = k + 1
        while j < self.n and t[k] >= t[j] - SHIFT_PAD_S:
            if j < len(core) and core[j]:
                return True
            j += 1
        return False

    def _row_flags(self, k, t):
        rpm, spd, ped = _v(self, "rpm", k), _v(self, "speed_kph", k), _v(self, "pedal_pct", k)
        f = 0
        if rpm > 400:
            f |= RUNNING
        if _v(self, "coolant_c", k) >= 80:
            f |= WARM
        if 0.98 <= _v(self, "lambda_cmd", k) <= 1.02:
            f |= CLOSED_LOOP
        if spd == 0 and 600 <= rpm <= 1000:
            f |= IDLE
        if spd > 0:
            f |= MOVING
        if spd > 50 and 15 <= ped <= 35 and 1400 <= rpm <= 2600:
            f |= CRUISE
        if _v(self, "boost_psi", k) > 3:
            f |= BOOST
        if ped >= HARD_PEDAL and _v(self, "throttle_pct", k) >= HARD_THROTTLE:
            f |= HARD
        if self._in_shift(k, t):
            f |= SHIFT
        return f

    def _count(self, k):
        """Settled row k into the summary and the event detectors (the maps catch up after)."""
        self._sum.add(self, k)
        for det in self._detectors:
            det.step(self, k)

    def _caught_up(self):
        for grid in self._grids.values():
            grid.catch_up(self)


class Summary:
    """analysis/carpi_analysis.py summarize() and trim_bands(), one settled row at a time.
    Band values are kept for exact medians; NaN is skipped, as pandas does."""

    def __init__(self):
        self.bands = [{"seconds": 0.0, "stft": [], "ltft": [], "total": []} for _ in BANDS]
        self.idle_rpm = []
        self.dist_km = 0.0
        self.max_speed = None
        self.peak = None                   # (psi, rpm, pedal) at the highest boost
        self.min_timing = None             # (deg, rpm): lowest timing over 5 psi, shifts excluded
        self.max_iat = self.max_coolant = None

    def add(self, d, k):
        t, f = d.data["elapsed_s"], d.flags[k]
        dt = t[k] - t[k - 1] if k else 0.0
        rpm, spd, psi = _v(d, "rpm", k), _v(d, "speed_kph", k), _v(d, "boost_psi", k)
        if (f & STEADY) == STEADY:
            for band, (_, lo, hi) in zip(self.bands, BANDS):
                if lo <= rpm < hi:
                    band["seconds"] += dt
                    for key, c in (("stft", "stft_pct"), ("ltft", "ltft_pct"), ("total", "trim_total")):
                        v = _v(d, c, k)
                        if _ok(v):
                            band[key].append(v)
            if rpm < 1000 and spd == 0:
                self.idle_rpm.append(rpm)
        if _ok(spd):
            self.dist_km += spd * dt / 3600
            self.max_speed = spd if self.max_speed is None else max(self.max_speed, spd)
        if _ok(psi) and (self.peak is None or psi > self.peak[0]):
            self.peak = (psi, rpm, _v(d, "pedal_pct", k))
        tim = _v(d, "timing_deg", k)
        if _ok(tim) and psi > 5 and not f & SHIFT and (self.min_timing is None or tim < self.min_timing[0]):
            self.min_timing = (tim, rpm)
        iat, cool = _v(d, "iat_c", k), _v(d, "coolant_c", k)
        if _ok(iat) and (self.max_iat is None or iat > self.max_iat):
            self.max_iat = iat
        if _ok(cool) and (self.max_coolant is None or cool > self.max_coolant):
            self.max_coolant = cool

    def out(self, d):
        def med(v, nd):
            return round(statistics.median(v), nd) if v else None

        def rnd(v, nd):
            return round(v, nd) if v is not None and _ok(v) else None
        t = d.data.get("elapsed_s")
        return {
            "rows": d.n,
            "duration_s": round(t[d.n - 1], 1) if d.n else 0.0,
            "distance_km": round(self.dist_km, 2),
            "max_speed_kph": rnd(self.max_speed, 1),
            "idle_rpm": med(self.idle_rpm, 0),
            "trim_bands": [{"label": label, "seconds": round(b["seconds"], 1), "stft_pct": med(b["stft"], 2),
                            "ltft_pct": med(b["ltft"], 2), "total_pct": med(b["total"], 2)}
                           for (label, _, _), b in zip(BANDS, self.bands)],
            "peak_boost": None if self.peak is None else {
                "psi": round(self.peak[0], 2), "rpm": rnd(self.peak[1], 0), "pedal": rnd(self.peak[2], 1)},
            "min_timing_boost": None if self.min_timing is None else {
                "deg": round(self.min_timing[0], 1), "rpm": rnd(self.min_timing[1], 0)},
            "max_iat_c": rnd(self.max_iat, 1),
            "max_coolant_c": rnd(self.max_coolant, 1),
        }


class Span:
    """Rows a..b of a drive, for an event's detail text. NaN is skipped, as pandas does."""

    def __init__(self, d, a, b):
        self.d, self.a, self.b = d, a, b
        t = d.data["elapsed_s"]
        self.t0, self.t1 = t[a], t[b]

    def vals(self, col, no_shift=False):
        arr, flags = self.d.data.get(col), self.d.flags
        if arr is None:
            return []
        return [arr[i] for i in range(self.a, self.b + 1)
                if _ok(arr[i]) and not (no_shift and flags[i] & SHIFT)]

    def first(self, col):
        return _v(self.d, col, self.a)

    def min(self, col, no_shift=False):
        v = self.vals(col, no_shift)
        return min(v) if v else NAN

    def max(self, col):
        v = self.vals(col)
        return max(v) if v else NAN

    def median(self, col):
        v = self.vals(col)
        return statistics.median(v) if v else NAN


class Detector:
    """One carpi_app/events.py detector: its row test, spans() limits and detail text, fed one
    settled row at a time. A span closes once a row comes more than gap_s after its last hit."""

    def __init__(self, kind, label, level, hit, min_s, min_rows, gap_s, text):
        self.kind, self.label, self.level, self.hit = kind, label, level, hit
        self.min_s, self.min_rows, self.gap_s, self.text = min_s, min_rows, gap_s, text
        self.run = None                    # [first row, last hit row]
        self.found = []                    # finished events

    def step(self, d, k):
        t = d.data["elapsed_s"]
        if self.hit(d, k):
            # back-to-back hits are one run; a run that starts within gap_s of the last merges
            if self.run and (k == self.run[1] + 1 or t[k] - t[self.run[1]] <= self.gap_s):
                self.run[1] = k
            else:
                self.close(d)
                self.run = [k, k]
        elif self.run and t[k] - t[self.run[1]] > self.gap_s:
            self.close(d)

    def close(self, d):
        if self.run and self._long_enough(d, *self.run):
            self.found.append(self._event(d, *self.run, ongoing=False))
        self.run = None

    def events(self, d):
        out = list(self.found)
        if self.run and not d.done and self._long_enough(d, *self.run):
            out.append(self._event(d, *self.run, ongoing=True))
        return out

    def _long_enough(self, d, a, b):
        t = d.data["elapsed_s"]
        return t[b] - t[a] >= self.min_s and b - a + 1 >= self.min_rows

    def _event(self, d, a, b, ongoing):
        s = Span(d, a, b)
        return {"kind": self.kind, "level": self.level, "t0": round(s.t0, 1), "t1": round(s.t1, 1),
                "text": self.text(s), "ongoing": ongoing}


def _tidy(v):
    return round(v, 6)                     # 0.30000000000000004 -> 0.3


class Grid:
    """An ECU-style map (carpi_app/views/map3d.py): z in bins of x and y over the settled rows that
    pass the filters. Cells are sparse, at each axis' step; view() merges them to fit the phone."""

    def __init__(self, x, y, z, filters=()):
        self.x, self.y, self.z = x, y, z
        self.sx, self.sy = step(x), step(y)
        self.need = self.avoid = 0
        for f in filters:
            if f == "no_shift":
                self.avoid |= SHIFT
            else:
                self.need |= FILTERS[f]
        self.cells = {}                    # (ix, iy) -> [sum, rows, min, max]
        self.done = 0                      # settled rows already counted

    def catch_up(self, d):
        X, Y, Z, flags = d.data[self.x], d.data[self.y], d.data[self.z], d.flags
        cells, sx, sy, need, avoid = self.cells, self.sx, self.sy, self.need, self.avoid
        for k in range(self.done, d.settled):
            f = flags[k]
            if (f & need) != need or f & avoid:
                continue
            x, y, z = X[k], Y[k], Z[k]
            if not (_ok(x) and _ok(y) and _ok(z)):
                continue
            key = (math.floor(x / sx + 1e-9), math.floor(y / sy + 1e-9))
            c = cells.get(key)
            if c is None:
                cells[key] = [z, 1, z, z]
            else:
                c[0] += z
                c[1] += 1
                if z < c[2]:
                    c[2] = z
                if z > c[3]:
                    c[3] = z
        self.done = d.settled

    def view(self, stat="mean", max_bins=MAX_BINS, min_rows=MIN_ROWS):
        """The map as sent to the phone: at most max_bins × max_bins cells (pairs of bins merge and
        the step doubles until it fits); cells under min_rows rows left out."""
        if stat not in STATS:
            raise ValueError(f"stat: one of {', '.join(STATS)}")
        rows = sum(c[1] for c in self.cells.values())
        fx = fy = 1
        while True:
            merged = {}
            for (ix, iy), (s, n, lo, hi) in self.cells.items():
                c = merged.setdefault((ix // fx, iy // fy), [0.0, 0, math.inf, -math.inf])
                c[0] += s
                c[1] += n
                c[2] = min(c[2], lo)
                c[3] = max(c[3], hi)
            shown = {k: c for k, c in merged.items() if c[1] >= min_rows}
            if not shown:
                return {"x": {"col": self.x, "step": self.sx, "start": 0},
                        "y": {"col": self.y, "step": self.sy, "start": 0},
                        "nx": 0, "ny": 0, "stat": stat, "cells": [], "rows": rows}
            xs, ys = [k[0] for k in shown], [k[1] for k in shown]
            wide = max(xs) - min(xs) + 1 > max_bins
            tall = max(ys) - min(ys) + 1 > max_bins
            if not (wide or tall):
                break
            fx, fy = fx * (2 if wide else 1), fy * (2 if tall else 1)
        x0, y0 = min(xs), min(ys)
        pick = {"mean": lambda c: float(f"{c[0] / c[1]:.4g}"), "min": lambda c: float(f"{c[2]:.4g}"),
                "max": lambda c: float(f"{c[3]:.4g}"), "rows": lambda c: c[1]}[stat]
        return {
            "x": {"col": self.x, "step": _tidy(self.sx * fx), "start": _tidy(x0 * self.sx * fx)},
            "y": {"col": self.y, "step": _tidy(self.sy * fy), "start": _tidy(y0 * self.sy * fy)},
            "nx": max(xs) - x0 + 1, "ny": max(ys) - y0 + 1, "stat": stat,
            "cells": [[ix - x0, iy - y0, pick(c), c[1]] for (ix, iy), c in sorted(shown.items())],
            "rows": rows,
        }


def _hit_pull(d, k):
    return (_v(d, "pedal_pct", k) >= HARD_PEDAL and _v(d, "throttle_pct", k) >= HARD_THROTTLE
            and _v(d, "rpm", k) > 1500)


def _hit_timing_dip(d, k):
    return _v(d, "timing_deg", k) <= -5 and _v(d, "boost_psi", k) > 5 and not d.flags[k] & SHIFT


def _hit_lean_boost(d, k):
    return (_v(d, "lambda", k) > 1.08 and _v(d, "lambda_cmd", k) <= 1.02 and _v(d, "boost_psi", k) > 5
            and not d.flags[k] & SHIFT)


def _hit_low_volts(d, k):
    return _v(d, "volts", k) < 12.4 and _v(d, "rpm", k) > 400


def _hit_high_trim(d, k):
    return (abs(_v(d, "trim_total", k)) > 12 and _v(d, "coolant_c", k) >= 80
            and 0.98 <= _v(d, "lambda_cmd", k) <= 1.02)


def _hit_long_idle(d, k):
    return _v(d, "speed_kph", k) == 0 and 500 <= _v(d, "rpm", k) <= 1100


# carpi_app/events.py, same thresholds, spans() limits and texts:
# (kind, label, level, row test, min_s, min_rows, merge gap s, detail text)
DETECTORS = [
    ("pull", "Hard acceleration", "info", _hit_pull, 1.0, 1, 0.8,
     lambda s: f"{s.first('rpm'):,.0f}→{s.max('rpm'):,.0f} rpm, peak {s.max('boost_psi'):.1f} psi, "
               f"min timing {s.min('timing_deg', True):.1f}°, min λ {s.min('lambda', True):.3f} (shifts excluded)"),
    ("timing_dip", "Timing ≤ -5° under boost", "warn", _hit_timing_dip, 0.0, 2, 0.5,
     lambda s: f"min {s.min('timing_deg'):.1f}° at {s.median('rpm'):,.0f} rpm, {s.max('boost_psi'):.1f} psi"),
    ("lean_boost", "Lean under boost", "crit", _hit_lean_boost, 0.0, 3, 0.0,
     lambda s: f"λ up to {s.max('lambda'):.3f} (cmd {s.median('lambda_cmd'):.3f})"),
    ("low_volts", "Voltage below 12.4 V", "warn", _hit_low_volts, 60.0, 1, 10.0,
     lambda s: f"min {s.min('volts'):.2f} V for {(s.t1 - s.t0) / 60:.1f} min"),
    ("high_trim", "Fuel trim beyond ±12%", "warn", _hit_high_trim, 10.0, 1, 3.0,
     lambda s: f"{s.median('trim_total'):+.1f}% median"),
    ("long_idle", "Idle longer than 3 min", "info", _hit_long_idle, 180.0, 1, 5.0,
     lambda s: f"{(s.t1 - s.t0) / 60:.1f} min, trim {s.median('trim_total'):+.1f}%"),
]
