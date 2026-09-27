#!/usr/bin/env python3
"""
build_site.py - build the drive viewer into _site/ for GitHub Pages.

Reads the curated drive list in data/drives.toml, analyzes each CSV, and writes
  _site/data/drives.json   every drive's title, summary and sensor checks (+ "upcoming")
  _site/data/<id>.json     one drive's 1-second trace and fuel-trim bands
next to a copy of web/. Exits non-zero with a clear message on any bad input, so
the GitHub Action never deploys a half-built site.

    python analysis/build_site.py [--out _site]
"""
import argparse
import datetime
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

import carpi_analysis as ca

REPO = Path(__file__).resolve().parent.parent
REQUIRED_KEYS = ["id", "file", "title", "date"]
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BUILD_MARKER = ".carpi-build"   # marks a folder build() created, so only those get deleted


class BuildError(Exception):
    pass


def read_manifest(path):
    path = Path(path)
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise BuildError(f"{path}: {e}") from e


def load_manifest(path):
    """The validated [[drive]] entries, each with an absolute `csv` path."""
    return validate_drives(read_manifest(path), Path(path))


def validate_drives(doc, path):
    """Check a parsed manifest's [[drive]] entries; `path` locates the CSVs and names errors."""
    drives = doc.get("drive", [])
    if not drives:
        raise BuildError(f"{path}: no [[drive]] entries")
    seen = set()
    for n, d in enumerate(drives, 1):
        missing = [k for k in REQUIRED_KEYS if k not in d]
        if missing:
            raise BuildError(f"{path.name}, drive #{n}: missing {', '.join(missing)}")
        if not SAFE_ID.match(str(d["id"])):
            raise BuildError(f"{path.name}, drive #{n}: id '{d['id']}' must be lowercase letters, digits and dashes")
        if d["id"] in seen:
            raise BuildError(f"{path.name}: duplicate id '{d['id']}'")
        seen.add(d["id"])
        d["csv"] = path.parent / d["file"]
        if not d["csv"].exists():
            raise BuildError(f"{path.name}, drive '{d['id']}': {d['file']} not found in {path.parent}")
        # a plain TOML date only: strings may not parse in the browser, and a
        # date-time would publish the time of day
        if type(d["date"]) is not datetime.date:
            raise BuildError(f"{path.name}, drive '{d['id']}': date must be unquoted, like date = 2026-09-26")
        d["date"] = d["date"].isoformat()
    return drives


def _write_json(path, obj):
    # allow_nan=False turns any NaN that slipped through into a build error, not broken JSON
    path.write_text(json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                    encoding="utf-8")


def _check_out_dir(out_dir, sources):
    """The output folder is deleted and rebuilt, so it must never be (or contain, or sit inside)
    a source folder, and must never be a folder this script didn't create."""
    out = out_dir.resolve()
    for src in sources:
        src = src.resolve()
        if out == src or out in src.parents or src in out.parents:
            raise BuildError(f"--out {out_dir} overlaps {src}; use a separate folder such as _site")
    if out.exists() and any(out.iterdir()) and not (out / BUILD_MARKER).exists():
        raise BuildError(f"--out {out_dir} is not a previous build (no {BUILD_MARKER}); refusing to delete it")


def build(manifest, web_dir, out_dir):
    doc = read_manifest(manifest)
    drives = validate_drives(doc, Path(manifest))
    out_dir = Path(out_dir)
    _check_out_dir(out_dir, [Path(web_dir), Path(manifest).parent])
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(web_dir, out_dir)
    (out_dir / BUILD_MARKER).write_text("Built by analysis/build_site.py; safe to delete.\n", encoding="utf-8")
    (out_dir / "data").mkdir()
    index = []
    for d in drives:
        try:
            result = ca.analyze(d["csv"])
        except ca.DriveError as e:
            raise BuildError(f"drive '{d['id']}': {e}") from e
        index.append({
            "id": d["id"], "title": d["title"], "group": d.get("group", ""), "date": d["date"], "tag": d.get("tag", ""),
            "note": d.get("note", ""), "finding": d.get("finding", ""),
            "summary": result["summary"], "checks": result["checks"],
        })
        _write_json(out_dir / "data" / f"{d['id']}.json",
                    {"trace": result["trace"], "bands": result["bands"]})
    _write_json(out_dir / "data" / "drives.json",
                {"drives": index, "upcoming": doc.get("upcoming", "")})
    return index


def main():
    ap = argparse.ArgumentParser(description="Build the CarPi drive viewer")
    ap.add_argument("--manifest", default=REPO / "data" / "drives.toml", type=Path)
    ap.add_argument("--web", default=REPO / "web", type=Path)
    ap.add_argument("--out", default=REPO / "_site", type=Path)
    args = ap.parse_args()
    try:
        index = build(args.manifest, args.web, args.out)
    except BuildError as e:
        sys.exit(f"build failed: {e}")
    print(f"built {len(index)} drive(s) -> {args.out}")


if __name__ == "__main__":
    main()
