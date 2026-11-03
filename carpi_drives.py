#!/usr/bin/env python3
"""
carpi_drives.py - the drives on the Pi, for the phone page's Analysis tab (protocol 6): the list
of saved drives, the analysis of one of them (carpi_stats.py), and the drive being recorded,
followed as it grows. carpi_control's backend for "drives", "analysis" and "analysis_map".

One background thread does the work: every FOLLOW_EVERY_S it reads the new lines of the drive the
logger is writing, loads a saved drive when the phone asks for one, builds the maps the phone
asked for, and publishes the results. Requests only read what it published, so Bluetooth never
waits on a big file. It runs at nice 10, below the logger and Bluetooth. Nothing is written to
disk: ~/carpi/logs is the carpi-data checkout, and the upload safety check refuses extra files.
"""
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import carpi_presets
import carpi_stats
import carpi_sync

LOGS = Path("~/carpi/logs").expanduser()
STATUS_FILE = Path("/run/carpi/status.json")
# Drive names, as deploy/check_drives.py allows them: day folders, and the older flat names
DRIVE_ID = re.compile(r"(\d{4}-\d{2}-\d{2})/drive-(\d{6})(?:-(\d+))?")
LEGACY_ID = re.compile(r"drive-(\d{4}-\d{2}-\d{2})_(\d{6})")
PAGE = 15                                  # drives per "drives" reply
EVENTS_SHOWN = 12                          # newest events per "analysis" reply (replies < 4 KB)
MAX_MAPS = 12                              # map requests remembered (both held drives)
FOLLOW_EVERY_S = 2.0
CHUNK = 1 << 20
NICE = 10


def drive_key(drive_id):
    """(day, HHMMSS, suffix) for a drive id, oldest first when sorted; None if it isn't one."""
    if not isinstance(drive_id, str):
        return None
    m = DRIVE_ID.fullmatch(drive_id)
    if m:
        return (m[1], m[2], int(m[3] or 0))
    m = LEGACY_ID.fullmatch(drive_id)
    return (m[1], m[2], 0) if m else None


def drive_path(logs, drive_id):
    """The CSV for a drive id, or ValueError. Only drive names inside logs: nothing else is readable."""
    if drive_key(drive_id) is None:
        raise ValueError("no such drive")
    root = Path(logs).resolve()
    path = (root / f"{drive_id}.csv").resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError("no such drive")
    return path


def list_drives(logs, before=None, current=None, page=PAGE):
    """Saved drives, newest first, `page` at a time, starting after `before`: {drives, more, current}."""
    logs = Path(logs)
    if before is not None and drive_key(before) is None:
        raise ValueError("before: a drive id")
    ids = []
    for pattern in ("*/drive-*.csv", "drive-*.csv"):
        for p in logs.glob(pattern):
            drive_id = p.relative_to(logs).with_suffix("").as_posix()
            if drive_key(drive_id):
                ids.append(drive_id)
    ids.sort(key=drive_key, reverse=True)
    if before is not None:
        ids = [i for i in ids if drive_key(i) < drive_key(before)]
    return {"drives": [_entry(logs, i) for i in ids[:page]], "more": len(ids) > page, "current": current}


def _entry(logs, drive_id):
    meta = carpi_presets.read_json(logs / f"{drive_id}.json")
    meta = meta if isinstance(meta, dict) else {}
    day, hhmmss, _ = drive_key(drive_id)
    preset = meta.get("preset")
    return {"id": drive_id, "day": day, "time": f"{hhmmss[:2]}:{hhmmss[2:4]}",
            "duration_s": meta.get("duration_s"), "rows": meta.get("rows"), "tag": meta.get("tag") or None,
            "preset": preset.get("name") if isinstance(preset, dict) else None,
            "complete": meta.get("complete") is not False, "private": meta.get("publish") is False}


def _map_spec(x, y, z, filters, stat):
    """A map request checked for shape (the worker checks the columns against the drive)."""
    for name, c in (("x", x), ("y", y), ("z", z)):
        if not isinstance(c, str) or not 0 < len(c) <= 40:
            raise ValueError(f"{name}: a column name")
    if not isinstance(filters, list) or any(f not in carpi_stats.FILTERS for f in filters):
        raise ValueError(f"filters: a list of {', '.join(carpi_stats.FILTERS)}")
    if stat not in carpi_stats.STATS:
        raise ValueError(f"stat: one of {', '.join(carpi_stats.STATS)}")
    return {"x": x, "y": y, "z": z, "filters": sorted(set(filters)), "stat": stat}


def _map_key(spec):
    return "|".join((spec["x"], spec["y"], spec["z"], ",".join(spec["filters"]), spec["stat"]))


class _Held:
    """A drive the worker holds in memory: its file, how far it has been read, its analysis."""

    def __init__(self, drive_id, path):
        self.id, self.path = drive_id, path
        self.drive = carpi_stats.Drive()
        self.offset = 0
        self.version = 0                   # bumped when rows arrive or the drive ends
        self.error = None if path is not None else "no such drive"
        self.grew_at = time.monotonic()
        self.cached = None                 # ((version, live), analysis reply)
        self.maps = {}                     # map key -> (version, map reply)

    def read_more(self):
        """Feed whatever the file gained since the last read."""
        if self.error:
            return
        rows = self.drive.n
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                while chunk := f.read(CHUNK):
                    self.offset += len(chunk)
                    self.drive.feed(chunk.decode("utf-8", errors="replace"))
        except (OSError, ValueError) as e:
            self.error = getattr(e, "strerror", None) or str(e)
            self.version += 1
        if self.drive.n > rows:
            self.version += 1
            self.grew_at = time.monotonic()

    def finish(self):
        if not self.error and not self.drive.done:
            self.drive.finish()
            self.version += 1


class Drives:
    """carpi_control's backend for the Analysis tab. start() runs the worker thread; step() is one
    round of its work (tests call it directly)."""

    def __init__(self, logs=LOGS, status_file=STATUS_FILE):
        self.logs, self.status_file = Path(logs), Path(status_file)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._live = None                  # the drive being recorded, or the last one this boot
        self._saved = None                 # the saved drive the phone opened
        self._wanted = None                # saved drive id asked for
        self._requests = OrderedDict()     # (drive id, map key) -> spec, newest last
        self._published = {}               # drive id -> {"analysis", "maps", "grew_at"}
        self._live_id = None
        self._recording = False

    # ---- the worker ----

    def start(self):
        self._thread = threading.Thread(target=self._run, name="carpi-drives", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)    # this thread (Linux)
        except (AttributeError, OSError):
            pass
        while not self._stop.is_set():
            self.step()
            self._wake.wait(FOLLOW_EVERY_S)
            self._wake.clear()

    def step(self):
        """Follow the live drive, load a wanted saved drive, publish both."""
        for part in (self._follow, self._load_wanted, self._publish):
            try:
                part()
            except Exception as e:             # one bad file must never stop the worker
                print(f"carpi_drives: {part.__name__}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    def _follow(self):
        rel = carpi_sync.current_drive(self.status_file)         # fresh status.json, state logging
        drive_id = rel[:-4] if rel and rel.endswith(".csv") else None
        live = self._live
        if drive_key(drive_id) is None:
            if live:
                live.finish()                                    # the logger stopped: it ended
            return
        if live is None or live.id != drive_id:
            if live:
                live.finish()
            live = self._live = _Held(drive_id, self.logs / rel)
        live.read_more()

    def _load_wanted(self):
        want = self._wanted
        if want is None or (self._live and self._live.id == want) or (self._saved and self._saved.id == want):
            return
        try:
            path = drive_path(self.logs, want)
        except ValueError:
            path = None
        held = _Held(want, path)
        held.read_more()
        held.finish()
        self._saved = held

    def _publish(self):
        with self._lock:
            requests = list(self._requests.items())
        out = {}
        for held in (self._live, self._saved):
            if held is None or held.id in out:
                continue
            live = held is self._live
            maps = {key: self._map(held, spec) for (drive_id, key), spec in requests if drive_id == held.id}
            out[held.id] = {"analysis": self._analysis(held, live), "maps": maps, "grew_at": held.grew_at}
        with self._lock:
            self._published = out
            self._live_id = self._live.id if self._live else None
            self._recording = bool(self._live and not self._live.error and not self._live.drive.done)

    def _analysis(self, held, live):
        key = (held.version, live)
        if held.cached and held.cached[0] == key:
            return held.cached[1]
        out = None
        if not held.error:
            d = held.drive
            try:
                summary, events = d.summary(), d.events()
            except Exception as e:
                held.error = f"{type(e).__name__}: {e}"
            else:
                rows, duration = summary.pop("rows"), summary.pop("duration_s")
                out = {"id": held.id, "live": live, "recording": live and not d.done, "state": "ready",
                       "error": None, "rows": rows, "duration_s": duration, "columns": d.columns(),
                       "summary": summary, "events": events[-EVENTS_SHOWN:], "events_total": len(events)}
        if out is None:
            out = {"id": held.id, "live": live, "recording": False, "state": "error", "error": held.error}
        held.cached = (key, out)
        return out

    def _map(self, held, spec):
        key = _map_key(spec)
        hit = held.maps.get(key)
        if hit and hit[0] == held.version:
            return hit[1]
        if held.error:
            out = {"id": held.id, "state": "error", "error": held.error}
        else:
            try:
                view = held.drive.grid(spec["x"], spec["y"], spec["z"], spec["filters"]).view(spec["stat"])
                out = dict(view, id=held.id, state="ready")
            except ValueError as e:
                out = {"id": held.id, "state": "error", "error": str(e)}
        held.maps[key] = (held.version, out)
        return out

    # ---- requests (the Bluetooth thread; never waits on the worker) ----

    def drives(self, before=None):
        with self._lock:
            current = self._live_id if self._recording else None
        return list_drives(self.logs, before, current)

    def analysis(self, drive="live"):
        drive_id = self._resolve(drive)
        if drive_id is None:
            return {"id": None, "live": True, "recording": False, "state": "none"}
        with self._lock:
            pub = self._published.get(drive_id)
        if pub is None:
            self._want(drive_id)
            return {"id": drive_id, "live": False, "recording": False, "state": "loading"}
        out = dict(pub["analysis"])
        if out.get("recording"):
            out["updated_s"] = round(time.monotonic() - pub["grew_at"], 1)
        return out

    def analysis_map(self, drive="live", x=None, y=None, z=None, filters=(), stat="mean"):
        spec = _map_spec(x, y, z, filters, stat)
        drive_id = self._resolve(drive)
        if drive_id is None:
            return {"id": None, "state": "none"}
        key = _map_key(spec)
        with self._lock:
            self._requests.pop((drive_id, key), None)
            self._requests[(drive_id, key)] = spec
            while len(self._requests) > MAX_MAPS:
                self._requests.popitem(last=False)
            pub = self._published.get(drive_id)
            found = pub["maps"].get(key) if pub else None
        if found is None:
            if pub is None:
                self._want(drive_id)
            self._wake.set()
            return {"id": drive_id, "state": "loading"}
        return found

    def _resolve(self, drive):
        if drive is None or drive == "live":
            with self._lock:
                return self._live_id
        if drive_key(drive) is None:
            raise ValueError("no such drive")
        return drive

    def _want(self, drive_id):
        drive_path(self.logs, drive_id)        # ValueError now, in the reply, not in the worker
        self._wanted = drive_id
        self._wake.set()
