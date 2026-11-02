"""deploy/check_drives.py: the safety check carpi-data's workflow runs before publishing anything."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))
import check_drives as cd  # noqa: E402

SAMPLE = ROOT / "data" / "2026-09-26"


@pytest.fixture
def tree(tmp_path):
    """A clean carpi-data checkout: the repo's own files and two real drives."""
    day = tmp_path / "2026-09-26"
    day.mkdir()
    for f in SAMPLE.glob("drive-*"):
        shutil.copy(f, day / f.name)
    (tmp_path / "README.md").write_text("carpi-data\n")
    (tmp_path / ".gitignore").write_text("*.tmp\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "publish.yml").write_text("name: x\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("anything goes in .git\n")
    return tmp_path


def _meta(tree, **changes):
    path = tree / "2026-09-26" / "drive-231349.json"
    meta = json.loads(path.read_text())
    meta.update(changes)
    path.write_text(json.dumps(meta))
    return path


def _only_problem(tree, fragment):
    found = cd.problems(tree)
    assert len(found) == 1 and fragment in found[0], found
    return found[0]


def test_clean_tree_passes(tree):
    assert cd.problems(tree) == []
    assert cd.main([str(tree)]) == 0


def test_legacy_flat_names_pass(tree):
    shutil.copy(SAMPLE / "drive-223818.csv", tree / "drive-2026-09-25_223818.csv")
    shutil.copy(SAMPLE / "drive-223818.json", tree / "drive-2026-09-25_223818.json")
    shutil.copy(SAMPLE / "drive-223818.csv", tree / "2026-09-26" / "drive-223818-2.csv")
    assert cd.problems(tree) == []


def test_power_cut_tail_passes(tree):
    path = tree / "2026-09-26" / "drive-223818.csv"
    with open(path, "ab") as f:
        f.write(b"2026-09-25T22:59:01.120,1243.0,0,81")        # a line cut off mid-write
        f.write(b"\x00" * 4096)                                 # and the block it was in
    assert cd.problems(tree) == []


def test_new_metadata_keys_pass(tree):
    _meta(tree, start_corrected="2026-09-26T18:13:49", utc_offset="-04:00", publish=False)
    assert cd.problems(tree) == []


@pytest.mark.parametrize("name", ["evil.html", "2026-09-26/notes.txt", "2026-09-26/drive-231349.csv.tmp",
                                  ".github/workflows/other.yml", "data/drive-231349.csv"])
def test_unexpected_paths_fail(tree, name):
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hello")
    _only_problem(tree, name)


def test_unknown_column_fails(tree):
    path = tree / "2026-09-26" / "drive-223818.csv"
    text = path.read_text()
    path.write_text(text.replace("time,elapsed_s", "time,script_src", 1))
    _only_problem(tree, "unknown column 'script_src'")


@pytest.mark.parametrize("bad", ["<b>hi</b>", "nan", "inf", "12abc"])
def test_text_in_a_number_column_fails(tree, bad):
    path = tree / "2026-09-26" / "drive-223818.csv"
    lines = path.read_text().splitlines(keepends=True)
    cells = lines[5].split(",")
    cells[3] = bad                                              # rpm
    lines[5] = ",".join(cells)
    path.write_text("".join(lines))
    _only_problem(tree, "line 6")


def test_odd_time_value_fails(tree):
    path = tree / "2026-09-26" / "drive-223818.csv"
    lines = path.read_text().splitlines(keepends=True)
    lines[3] = '<script>alert(1)</script>' + lines[3][lines[3].index(","):]
    path.write_text("".join(lines))
    _only_problem(tree, "line 4")


def test_oversized_csv_fails(tree, monkeypatch):
    monkeypatch.setattr(cd, "MAX_CSV", 1000)
    found = cd.problems(tree)
    assert len(found) == 2 and all("too big" in p for p in found)


@pytest.mark.parametrize("meta, fragment", [
    ([1, 2], "not a JSON object"),
    ({"start": "2026-09-26T18:13:49", "html": "<b>"}, "unknown key 'html'"),
    ({"start": "x" * 1000}, "'start'"),
    ({"start": "2026-09-26T18:13:49", "publish": "no"}, "'publish'"),
    ({"start": "2026-09-26T18:13:49", "utc_offset": "EST"}, "'utc_offset'"),
    ({"start": "2026-09-26T18:13:49", "rows": "many"}, "'rows'"),
    ({"start": "2026-09-26T18:13:49", "weights": {"0C": "<b>"}}, "'weights'"),
    ({"start": "2026-09-26T18:13:49", "supported_pids": ["0C"] * 300}, "'supported_pids'"),
])
def test_bad_metadata_fails(tree, meta, fragment):
    (tree / "2026-09-26" / "drive-231349.json").write_text(json.dumps(meta))
    _only_problem(tree, fragment)


def test_unparseable_json_fails(tree):
    (tree / "2026-09-26" / "drive-231349.json").write_text("{not json")
    _only_problem(tree, "drive-231349.json")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_symlinks_fail(tree):
    link = tree / "2026-09-26" / "drive-000001.csv"
    try:
        os.symlink(tree / "README.md", link)
    except OSError:
        pytest.skip("can't create symlinks here")
    _only_problem(tree, "symlink")


def test_main_reports_and_fails(tree, capsys):
    (tree / "evil.html").write_text("<script>")
    assert cd.main([str(tree)]) == 1
    assert "evil.html" in capsys.readouterr().out
