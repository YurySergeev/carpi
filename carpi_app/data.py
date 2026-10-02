"""Finding, loading and caching drive logs."""
from __future__ import annotations
import hashlib
import io
import json
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, filters
from .channels import DERIVED, add_derived

SUMMARY_VERSION = 2


@dataclass
class DriveInfo:
    id: str               # path relative to its log folder, without .csv (unique)
    path: str
    tag: str
    start: str | None     # ISO start time, corrected when the Pi clock synced mid-drive
    start_ok: bool        # False = the Pi clock was never synced; start is a guess
    duration_min: float
    distance_mi: float
    rows: int
    max_rpm: float
    peak_boost_psi: float
    mtime: float
    size: int

    @property
    def label(self):
        when = pd.Timestamp(self.start).strftime("%a %b %d  %H:%M") if self.start else self.id
        star = "" if self.start_ok else "?"
        return f"{when}{star}  ·  {self.duration_min:.0f} min  ·  {self.distance_mi:.1f} mi  ·  {self.tag}"


# ------------------------------------------------------------------ reading
def read_csv(path):
    """Tolerant read: skips bad lines, drops a half-written last row, numeric everything."""
    raw = Path(path).read_bytes()
    df = pd.read_csv(io.BytesIO(raw), on_bad_lines="skip", low_memory=False)
    if raw and not raw.endswith(b"\n") and len(df):
        df = df.iloc[:-1]
    times = pd.to_datetime(df["time"], errors="coerce") if "time" in df.columns else None
    for c in df.columns:
        if c != "time":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["elapsed_s"])
    if times is not None:
        times = times.loc[df.index]
    order = df["elapsed_s"].argsort(kind="stable")
    df = df.iloc[order].reset_index(drop=True)
    if times is not None:
        times = times.iloc[order].reset_index(drop=True)
    return df, times


def start_time(df, times):
    """Same rule as analyze_drives.py: use the first clock_ok row, else the last clock jump."""
    if times is None or not times.notna().any():
        return None, False
    if "clock_ok" in df.columns and (df["clock_ok"] == 1).any():
        i = df.index[df["clock_ok"] == 1][0]
        return times[i] - pd.Timedelta(seconds=float(df["elapsed_s"][i])), True
    jump = (times.diff().dt.total_seconds() - df["elapsed_s"].diff()).abs()
    big = jump[jump > 2]
    if len(big):
        i = big.index[-1]
        return times[i] - pd.Timedelta(seconds=float(df["elapsed_s"][i])), True
    return times.dropna().iloc[0], False


def fingerprint(path):
    p = Path(path)
    h = hashlib.md5()
    with p.open("rb") as f:
        h.update(f.read(256 * 1024))
    return f"{p.stat().st_size}-{h.hexdigest()}"


def prepare(df):
    """Forward-fill round-robin PIDs, float32 to halve memory, add derived channels."""
    df = df.drop(columns=[c for c in ("time",) if c in df.columns]).ffill()
    for c in df.columns:
        if c != "elapsed_s":
            df[c] = df[c].astype("float32")
    return add_derived(df)


# ------------------------------------------------------------------ store
class Store:
    def __init__(self, dirs=None):
        self.dirs = [Path(d) for d in (dirs or config.LOG_DIRS)]
        self.drives = {}            # id -> DriveInfo, ordered by start
        self._frames = OrderedDict()
        self._lock = threading.RLock()       # the hosted copy serves several requests at once
        self._cache_file = config.CACHE_DIR / "index.json"
        self.refresh()

    # ---- discovery + summaries
    def refresh(self):
        cache = {}
        if self._cache_file.exists():
            try:
                cache = json.loads(self._cache_file.read_text())
                if cache.get("_v") != SUMMARY_VERSION:
                    cache = {}
            except json.JSONDecodeError:
                cache = {}
        seen, found = set(), {}
        for root in self.dirs:
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("drive-*.csv")):
                fp = fingerprint(p)
                if fp in seen:          # identical copy in another folder
                    continue
                seen.add(fp)
                did = p.relative_to(root).with_suffix("").as_posix()
                if did in found:
                    did = p.relative_to(root.parent).with_suffix("").as_posix()
                st = p.stat()
                hit = cache.get(str(p))
                if hit and hit["mtime"] == st.st_mtime and hit["size"] == st.st_size:
                    info = DriveInfo(**{**hit, "id": did})
                else:
                    try:
                        info = self._summarize(p, did)
                    except Exception as ex:
                        print(f"  skipped {p.name}: {type(ex).__name__}: {ex}")
                        continue
                found[did] = info
        self.drives = dict(sorted(found.items(), key=lambda kv: (kv[1].start or "", kv[0])))
        try:
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            out = {"_v": SUMMARY_VERSION}
            out.update({i.path: asdict(i) for i in self.drives.values()})
            self._cache_file.write_text(json.dumps(out, indent=1))
        except OSError as ex:      # read-only disk: fine, we just re-summarize next start
            print(f"  summary cache not written: {ex}")
        return self.drives

    def _summarize(self, p, did):
        df, times = read_csv(p)
        start, ok = start_time(df, times)
        meta = {}
        side = p.with_suffix(".json")
        if side.exists():
            try:
                meta = json.loads(side.read_text())
            except json.JSONDecodeError:
                pass
        f = prepare(df)
        dt = f["elapsed_s"].diff().fillna(0).clip(0, 1.0)
        st = p.stat()
        return DriveInfo(
            id=did, path=str(p), tag=meta.get("tag") or "untagged",
            start=start.isoformat(timespec="seconds") if start is not None else None, start_ok=ok,
            duration_min=round(float(f["elapsed_s"].max() or 0) / 60, 1),
            distance_mi=round(float((f["speed_kph"].fillna(0) * dt).sum() / 3600 * 0.621371), 1)
            if "speed_kph" in f else 0.0,
            rows=len(f), max_rpm=float(np.nan_to_num(f["rpm"].max())) if "rpm" in f else 0.0,
            peak_boost_psi=round(float(np.nan_to_num(f["boost_psi"].max())), 1),
            mtime=st.st_mtime, size=st.st_size)

    # ---- frames
    def frame(self, drive_id):
        info = self.drives[drive_id]
        key = (info.path, info.mtime)
        with self._lock:
            if key in self._frames:
                self._frames.move_to_end(key)
                return self._frames[key]
            df = self._load_prepared(info)
            self._frames[key] = df
            while len(self._frames) > config.FRAME_CACHE_SIZE:
                self._frames.popitem(last=False)
            return df

    def _disk_key(self, info):
        """Prepared-frame cache file. Changes when the CSV, the derived channels or the format change."""
        sig = f"{SUMMARY_VERSION}|{info.size}|{info.mtime}|{','.join(DERIVED)}|{pd.__version__}"
        return config.CACHE_DIR / "frames" / f"{Path(info.path).stem}-{hashlib.md5(sig.encode()).hexdigest()[:10]}.pkl"

    def _load_prepared(self, info):
        f = self._disk_key(info)
        if f.exists():
            try:
                return pd.read_pickle(f)       # our own cache file, written below
            except Exception:
                pass
        df, _ = read_csv(info.path)
        df = prepare(df)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            df.to_pickle(f)
        except OSError:
            pass
        return df

    def preload(self):
        """Parse every drive into memory (the hosted copy does this in the background at startup)."""
        for did in list(self.drives):
            try:
                self.frame(did)
            except Exception as ex:
                print(f"  preload failed for {did}: {ex}")

    def frames(self, ids, filter_keys=(), query=None, columns=None):
        """All selected drives stacked, after filters + query, plus three columns:
        order (position of the drive in `ids`, an int that's cheap to send to the browser),
        drive and tag (categoricals, so grouping 28 drives costs nothing).
        Returns (df, total_rows_before_filtering, error_message_or_None)."""
        parts, total, err = [], 0, None
        ids = [d for d in (ids or []) if d in self.drives]
        for i, did in enumerate(ids):
            f = self.frame(did)
            total += len(f)
            m, err = filters.mask(f, filter_keys, query)
            if columns is None:
                g = f[m].copy()
            else:
                keep = list(dict.fromkeys([c for c in columns if c in f.columns] + ["elapsed_s"]))
                g = f.loc[m, keep].copy()
            g["order"] = np.int16(i)
            parts.append(g)
        if not parts:
            return pd.DataFrame(), total, err
        df = pd.concat(parts, ignore_index=True)
        codes = df["order"].to_numpy()
        df["drive"] = pd.Categorical.from_codes(codes, categories=ids)
        tags = [self.drives[d].tag for d in ids]
        df["tag"] = pd.Categorical(np.asarray(tags, dtype=object)[codes], categories=list(dict.fromkeys(tags)))
        return df, total, err

    def drive_at(self, ids, order):
        """Inverse of the `order` column from frames(): the drive id at that position."""
        ids = [d for d in (ids or []) if d in self.drives]
        try:
            return ids[int(order)]
        except (IndexError, TypeError, ValueError):
            return None

    def columns(self):
        """Columns that have data in at least one loaded/known drive (uses the newest drive)."""
        if not self.drives:
            return []
        f = self.frame(list(self.drives)[-1])
        return [c for c in f.columns if f[c].notna().any()]

    def options(self):
        return [{"label": i.label, "value": i.id} for i in reversed(list(self.drives.values()))]

    def tags(self):
        return list(dict.fromkeys(i.tag for i in self.drives.values()))
