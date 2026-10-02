#!/usr/bin/env python3
"""Build the hosted copy of the CarPi Analyzer and publish it.

    python deploy/publish.py build                          # just build deploy/build/site and look at it
    python deploy/publish.py render                         # push to the GitHub repo Render deploys from
    python deploy/publish.py hf --repo NAME/carpi-analyzer  # Hugging Face Space (needs PRO since July 2026)

Render (free): the first `render` run asks for the repo URL (an EMPTY GitHub repo you created, e.g.
https://github.com/YurySergeev/carpi-live) and remembers it in deploy/build/.remote. Each run commits the
current app + drives there and pushes; Render redeploys automatically.

Every unique drive is bundled (identical copies skipped, drives under 10 rows left out).
Use --exclude "*2026-09-25*" (repeatable) to leave drives out, --include to keep only matches.
"""
import argparse
import fnmatch
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

BUILD = HERE / "build" / "site"
REMOTE_FILE = HERE / "build" / ".remote"
TEMPLATE = HERE / "template"
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".cache", "tests", ".pytest_cache")


def drive_dest(path, roots):
    """logs/logs/2026-10-01/drive-120255.csv -> 2026-10-01/drive-120255.csv (no machine paths leak)."""
    p = Path(path).resolve()
    for r in roots:
        try:
            rel = p.relative_to(Path(r).resolve())
        except ValueError:
            continue
        return Path(*[x for x in rel.parts if x not in ("logs", "data")])
    return Path(p.name)


def build(include, exclude):
    from carpi_app import config
    from carpi_app.data import Store

    BUILD.mkdir(parents=True, exist_ok=True)
    for child in BUILD.iterdir():            # keep .git so the Render repo history survives rebuilds
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
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
    print(f"Built {BUILD}  ({n} drives, {size / 1e6:.1f} MB of logs)")
    return n


def git(*args, check=True):
    r = subprocess.run(["git", "-C", str(BUILD), *args], text=True, capture_output=True)
    if check and r.returncode:
        sys.exit(f"git {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
    return r


def publish_render(remote):
    if not remote:
        remote = REMOTE_FILE.read_text().strip() if REMOTE_FILE.exists() else ""
    if not remote:
        remote = input("GitHub repo URL to publish to (create an EMPTY repo first, e.g. "
                       "https://github.com/YurySergeev/carpi-live): ").strip()
    if not remote:
        sys.exit("No repo URL given.")
    REMOTE_FILE.write_text(remote)
    if not (BUILD / ".git").exists():
        git("init", "-b", "main")
    if git("remote", "get-url", "origin", check=False).returncode:
        git("remote", "add", "origin", remote)
    else:
        git("remote", "set-url", "origin", remote)
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        print("Nothing changed since the last publish.")
        return
    git("commit", "-m", f"Publish {datetime.now():%Y-%m-%d %H:%M}")
    print(f"Pushing to {remote} ...")
    r = subprocess.run(["git", "-C", str(BUILD), "push", "-u", "origin", "main", "--force"])
    if r.returncode:
        sys.exit("Push failed (see above). Check the repo URL and that you can push to it.")
    print("\nPushed. If this is the first time: Render dashboard > New > Blueprint > pick this repo > Apply.\n"
          "After that, Render redeploys on every publish (about 3-5 minutes). Your link is shown on the\n"
          "service page, e.g. https://carpi-analyzer.onrender.com")


def publish_hf(repo):
    try:
        from huggingface_hub import HfApi, get_token, login
    except ImportError:
        sys.exit("Install the uploader first:  pip install huggingface_hub")
    if not get_token():
        print("Paste a Hugging Face token with WRITE access (huggingface.co/settings/tokens):")
        login()
    api = HfApi()
    try:
        url = api.create_repo(repo, repo_type="space", space_sdk="docker", exist_ok=True)
    except Exception as ex:
        if "402" in str(ex):
            sys.exit("Hugging Face wants a paid plan for Docker Spaces (since July 2026). Use `render` instead.")
        raise
    print(f"Uploading to {url} ...")
    api.upload_folder(folder_path=str(BUILD), repo_id=repo, repo_type="space",
                      commit_message="Publish CarPi Analyzer", delete_patterns="*", ignore_patterns=[".git/*"])
    user, name = repo.split("/", 1)
    print(f"\nDone. Live in a few minutes at https://{user.lower()}-{name.lower()}.hf.space")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", choices=["build", "render", "hf"])
    ap.add_argument("--remote", help="render: GitHub repo URL (remembered after the first run)")
    ap.add_argument("--repo", help="hf: Space id, e.g. yourname/carpi-analyzer")
    ap.add_argument("--include", action="append", default=[], help="only drives matching this glob (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], help="skip drives matching this glob (repeatable)")
    a = ap.parse_args()
    if a.target == "hf" and not a.repo:
        ap.error("hf needs --repo NAME/carpi-analyzer")
    if not build(a.include, a.exclude):
        sys.exit("No drives matched; nothing to publish.")
    if a.target == "render":
        publish_render(a.remote)
    elif a.target == "hf":
        publish_hf(a.repo)


if __name__ == "__main__":
    main()
