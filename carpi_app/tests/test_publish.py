"""deploy/publish.py: building from a given folder (the carpi-data checkout), and private drives.
Local only (CI has no pandas). Run from the CarPi folder:  python -m pytest carpi_app/tests -q"""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))
import publish  # noqa: E402

SAMPLE = ROOT / "data" / "2026-09-26"


@pytest.fixture
def logs(tmp_path, monkeypatch):
    from carpi_app import config
    monkeypatch.setattr(publish, "BUILD", tmp_path / "build" / "site")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    day = tmp_path / "drives" / "2026-09-26"
    day.mkdir(parents=True)
    for f in SAMPLE.glob("drive-*"):
        shutil.copy(f, day / f.name)
    return tmp_path / "drives"


def bundle():
    return sorted(p.relative_to(publish.BUILD / "drives").as_posix() for p in (publish.BUILD / "drives").rglob("*"))


def test_builds_from_the_given_folder_only(logs):
    assert publish.build([], [], dirs=[logs]) == 2
    assert bundle() == ["2026-09-26", "2026-09-26/drive-223818.csv", "2026-09-26/drive-223818.json",
                        "2026-09-26/drive-231349.csv", "2026-09-26/drive-231349.json"]


def test_private_drives_are_left_out(logs, capsys):
    meta_path = logs / "2026-09-26" / "drive-231349.json"
    meta = json.loads(meta_path.read_text())
    meta_path.write_text(json.dumps(dict(meta, publish=False)))
    assert publish.build([], [], dirs=[logs]) == 1
    assert "drive-231349.csv" not in " ".join(bundle())
    assert "left out 1 private drive" in capsys.readouterr().out
