#!/usr/bin/env python3
"""Build the hosted copy of the CarPi Analyzer and publish it to a Hugging Face Space.

    python deploy/publish_space.py --build-only               # just build deploy/build/space and look at it
    python deploy/publish_space.py --repo YOUR_HF_NAME/carpi-analyzer

The first publish creates the Space (public, Docker, free CPU). Later runs replace its contents with
the current app and drives, so after copying new logs from the Pi you just run the same command again.
Needs:  pip install huggingface_hub   and a Hugging Face *write* token (you're asked for it once).

Only drives the app can read are bundled: identical copies are skipped, and drives under 10 rows are left out.
Change what's shown with --include / --exclude (glob on the drive path, e.g. --exclude "*2026-09-25*").
"""
import argparse
import fnmatch
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

BUILD = HERE / "build" / "space"
TEMPLATE = HERE / "space-template"
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".cache", "tests", ".pytest_cache")


def drive_dest(path, roots):
    """logs/logs/2026-10-01/drive-120255.csv -> 2026-10-01/drive-120255.csv (no machine paths leak)."""
    p = Path(path).resolve()
    for r in roots:
        try:
            rel = p.relative_to(Path(r).resolve())
        except ValueError:
            continue
        parts = [x for x in rel.parts if x not in ("logs", "data")]
        return Path(*parts)
    return Path(p.name)


def build(include, exclude):
    from carpi_app import config
    from carpi_app.data import Store

    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    shutil.copytree(TEMPLATE, BUILD, dirs_exist_ok=True)
    shutil.copytree(ROOT / "carpi_app", BUILD / "carpi_app", ignore=SKIP)

    store = Store(config.LOG_DIRS)
    n, size = 0, 0
    for info in store.drives.values():
        rel = drive_dest(info.path, store.dirs)
        key = rel.as_posix()
        if include and not any(fnmatch.fnmatch(key, g) for g in include):
            continue
        if any(fnmatch.fnmatch(key, g) for g in exclude) or info.rows < 10:
            continue
        dst = BUILD / "drives" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(info.path, dst)
        side = Path(info.path).with_suffix(".json")
        if side.exists():
            shutil.copy2(side, dst.with_suffix(".json"))
        n += 1
        size += dst.stat().st_size
    big = [p for p in (BUILD / "drives").rglob("*.csv") if p.stat().st_size > 10_000_000]
    print(f"Built {BUILD}  ({n} drives, {size / 1e6:.1f} MB of logs)")
    if big:
        print(f"  note: {len(big)} file(s) over 10 MB; huggingface_hub uploads them fine via Xet/LFS")
    return n


def publish(repo):
    try:
        from huggingface_hub import HfApi, get_token, login
    except ImportError:
        sys.exit("Install the uploader first:  pip install huggingface_hub")
    if not get_token():
        print("Paste a Hugging Face token with WRITE access (huggingface.co/settings/tokens):")
        login()
    api = HfApi()
    url = api.create_repo(repo, repo_type="space", space_sdk="docker", exist_ok=True, visibility="public")
    print(f"Uploading to {url} ...")
    api.upload_folder(folder_path=str(BUILD), repo_id=repo, repo_type="space",
                      commit_message="Publish CarPi Analyzer", delete_patterns="*")
    user, name = repo.split("/", 1)
    print(f"\nDone. The Space rebuilds in 2-4 minutes, then it's live at:\n"
          f"  https://huggingface.co/spaces/{repo}\n"
          f"  https://{user.lower()}-{name.lower().replace('_', '-').replace('.', '-')}.hf.space   (app only, best link to share)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="Space id, e.g. yurysergeev/carpi-analyzer")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--include", action="append", default=[], help="only drives matching this glob (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], help="skip drives matching this glob (repeatable)")
    a = ap.parse_args()
    if not a.build_only and not a.repo:
        ap.error("pass --repo YOUR_HF_NAME/carpi-analyzer, or --build-only")
    if not build(a.include, a.exclude):
        sys.exit("No drives matched; nothing to publish.")
    if not a.build_only:
        publish(a.repo)


if __name__ == "__main__":
    main()
