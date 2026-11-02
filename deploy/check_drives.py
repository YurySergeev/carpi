#!/usr/bin/env python3
"""check_drives.py FOLDER: the safety check carpi-data's workflow runs before publishing.

Exit 0 if every file in FOLDER is a plausible CarPi drive (or one of the repo's own three files),
otherwise print each problem and exit 1, so nothing gets published and the last good analyzer
stays up. The Pi's key can only write carpi-data; this check is what keeps anything that isn't a
drive (a page, a script, a link to some other file) away from the public analyzer.

A cut-off last line or NUL padding at the end of a CSV passes: that's normal power-cut damage.

Standard library + carpi_presets only.
"""
import csv
import io
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import carpi_presets  # noqa: E402

REPO_FILES = {"README.md", ".gitignore", ".github/workflows/publish.yml"}
DRIVE = re.compile(r"\d{4}-\d{2}-\d{2}/drive-\d{6}(-\d+)?\.(csv|json)")
LEGACY = re.compile(r"drive-\d{4}-\d{2}-\d{2}_\d{6}\.(csv|json)")
MAX_CSV, MAX_JSON = 20_000_000, 64_000
COLUMNS = {"time", "elapsed_s", "clock_ok"} | {c for *_, cols in carpi_presets.CATALOG.values() for c in cols}
TIME = re.compile(r"[0-9T:.+\- ]{1,32}")                 # 2026-09-25T22:38:18.173
OFFSET = re.compile(r"[+-]\d\d:\d\d")
SHORT, LONG = 16, 200


def _text(v, n=LONG):
    return v is None or (isinstance(v, str) and len(v) <= n)


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _short_list(v):
    return isinstance(v, list) and len(v) <= 256 and all(_text(x, SHORT) and x is not None for x in v)


META = {                       # key -> check; anything else in a drive's .json fails
    "start": _text, "end": _text, "start_corrected": _text, "tag": _text, "follows": _text,
    "request_id": lambda v: _text(v, SHORT), "ended_by": lambda v: _text(v, SHORT),
    "utc_offset": lambda v: isinstance(v, str) and OFFSET.fullmatch(v) is not None,
    "publish": lambda v: isinstance(v, bool), "complete": lambda v: isinstance(v, bool),
    "rows": _number, "duration_s": _number,
    "supported_pids": _short_list, "fast_pids": _short_list, "slow_pids": _short_list,
    "preset": lambda v: isinstance(v, dict) and set(v) <= {"id", "name"} and all(_text(x) for x in v.values()),
    "weights": lambda v: isinstance(v, dict) and len(v) <= 256
               and all(_text(k, SHORT) and _number(w) for k, w in v.items()),
}


def check_csv(data):
    """The first problem in a drive CSV, or None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8 text"
    text = text.rstrip("\x00")
    if "\x00" in text:
        return "NUL bytes inside the file"
    if text and not text.endswith("\n"):
        text = text[:text.rfind("\n") + 1]                   # drop a line cut off by a power cut
    rows = csv.reader(io.StringIO(text, newline=""))
    header = next(rows, None)
    if not header:
        return "empty"
    for name in header:
        if name not in COLUMNS:
            return f"unknown column {name[:40]!r}"
    if len(set(header)) != len(header):
        return "a column appears twice"
    for n, row in enumerate(rows, start=2):
        if len(row) > len(header):
            return f"line {n}: more values than columns"
        for name, value in zip(header, row):
            if not value:
                continue
            if name == "time":
                if not TIME.fullmatch(value):
                    return f"line {n}: odd time value"
                continue
            try:
                ok = math.isfinite(float(value))
            except ValueError:
                ok = False
            if not ok:
                return f"line {n}: {name} isn't a number"
    return None


def check_json(data):
    """The first problem in a drive's metadata, or None."""
    try:
        meta = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "not valid JSON"
    if not isinstance(meta, dict):
        return "not a JSON object"
    for key, value in meta.items():
        if key not in META:
            return f"unknown key {key[:40]!r}"
        if not META[key](value):
            return f"bad value for {key!r}"
    return None


def problems(root):
    """Every problem under root (except .git/), as "path: what's wrong"."""
    root = Path(root)
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if path.is_symlink():
            found.append(f"{rel}: a symlink")
            continue
        if path.is_dir():
            continue
        if rel in REPO_FILES:
            continue
        if not (DRIVE.fullmatch(rel) or LEGACY.fullmatch(rel)):
            found.append(f"{rel}: unexpected file")
            continue
        size = path.stat().st_size
        csv_file = rel.endswith(".csv")
        if size > (MAX_CSV if csv_file else MAX_JSON):
            found.append(f"{rel}: too big ({size:,} bytes)")
            continue
        problem = (check_csv if csv_file else check_json)(path.read_bytes())
        if problem:
            found.append(f"{rel}: {problem}")
    return found


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    found = problems(argv[0])
    for p in found:
        print(p)
    if found:
        print(f"{len(found)} problem(s): not publishing.")
        return 1
    n = sum(1 for p in Path(argv[0]).rglob("drive-*.csv"))
    print(f"OK: {n} drive(s) look like CarPi drives.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
