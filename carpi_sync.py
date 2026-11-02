#!/usr/bin/env python3
"""
carpi_sync.py - back up finished drives to the private GitHub repo carpi-data. A GitHub Action
there checks them and publishes them to the public analyzer. See docs/data-sync.md.

    python3 carpi_sync.py            one run: what carpi-sync.timer does every 2 minutes
    python3 carpi_sync.py --now      one run even with automatic uploads off, with output
    python3 carpi_sync.py --keygen   make the Pi's upload key; prints the public half for GitHub
    python3 carpi_sync.py --setup    make ~/carpi/logs a checkout of carpi-data and upload every drive

~/carpi/logs is a git checkout. Each run repairs what a power cut may have left behind (a stale
lock, a damaged .git), checks that GitHub answers, commits the finished drives (never the one
being written) and pushes. The result goes to /run/carpi/sync.json for the phone page.

The phone page's Uploads card writes ~/carpi/upload.json, {"auto": bool, "publish": bool}. With
"auto" off, timer runs stop before going online; Upload now (the Bluetooth service creates
/run/carpi/sync-now, which starts carpi-sync.path) still runs. "publish" is the logger's business:
it marks drives recorded with publishing off, and the GitHub side leaves those out.

Times in sync.json are seconds since boot, so the phone setting the clock doesn't make the last
upload look days old. Standard library + the git CLI; SSH uses its own key and a pinned GitHub
host key, never ~/.ssh/config.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import carpi_presets

REMOTE = "git@github.com:YurySergeev/carpi-data.git"
KEY = Path("~/.ssh/carpi_data").expanduser()
KNOWN_HOSTS = Path("~/.ssh/known_hosts_github").expanduser()
GITHUB_ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
GITHUB_FINGERPRINT = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"    # docs.github.com

LOGS = Path("~/carpi/logs").expanduser()
SETTINGS = Path("~/carpi/upload.json").expanduser()
SYNC_FILE = Path("/run/carpi/sync.json")
TRIGGER = Path("/run/carpi/sync-now")
STATUS_FILE = Path("/run/carpi/status.json")
EVERY_S, FIRST_S, STUCK_S = 120, 60, 600     # carpi-sync.timer; the service's TimeoutStartSec
LOCK_STALE_S, STATUS_FRESH_S = 60, 10
GITIGNORE = "*.tmp\n.git.broken-*/\n"
AUTHOR = ["-c", "user.name=CarPi Pi", "-c", "user.email=carpi@carpi.local"]
DRIVE_FILE = re.compile(r"(.*drive-[\d_-]+)\.(csv|json)")
# ssh/git errors that mean "no internet right now"; anything else is a real problem worth showing
OFFLINE = re.compile(r"could not resolve|name resolution|network is unreachable|no route to host|"
                     r"timed out|connection refused|connection reset|connection closed", re.I)
SETUP_HINT = "not set up: run  python3 ~/carpi/carpi_sync.py --setup"


class SyncError(Exception):
    pass


def uptime():
    """Seconds since boot (not the wall clock, which the phone may set at any moment)."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


# ---- settings from the phone page ----

def read_settings(path=SETTINGS):
    """{"auto", "publish"}: no file = both on (uploads as before). A file that can't be read, or a
    value that isn't true, turns publishing off: when in doubt, keep drives private."""
    path = Path(path)
    if not path.exists():
        return {"auto": True, "publish": True}
    data = carpi_presets.read_json(path)
    if not isinstance(data, dict):
        return {"auto": True, "publish": False}
    return {key: data.get(key, True) is True for key in ("auto", "publish")}


def write_settings(path, **changes):
    if not changes:
        raise ValueError("upload: give auto and/or publish (true or false)")
    for key, value in changes.items():
        if key not in ("auto", "publish") or not isinstance(value, bool):
            raise ValueError(f"upload: {key} must be true or false")
    settings = dict(read_settings(path), **changes)
    carpi_presets.atomic_write(path, json.dumps(settings))
    return settings


def write_status(path, **fields):
    """Merge fields into sync.json. Best effort: no /run/carpi just means no phone status."""
    data = carpi_presets.read_json(path)
    data = data if isinstance(data, dict) else {}
    data.update(fields)
    try:
        carpi_presets.atomic_write(path, json.dumps(data))
    except OSError:
        pass
    return data


# ---- git ----

def ssh_env():
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=(
        f"ssh -i {KEY} -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 "
        f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={KNOWN_HOSTS}"))


def git(logs, *args, env=None, timeout=60, check=True):
    name = next((a for a in args if not a.startswith("-") and "=" not in a), "git")
    try:
        r = subprocess.run(["git", "-C", str(logs), *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise SyncError(f"git {name} timed out") from None
    if check and r.returncode:
        raise SyncError(f"git {name}: {last_line(r.stderr or r.stdout)}")
    return r


def last_line(text):
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return (lines[-1] if lines else "failed")[:200]


def classify(stderr):
    """("waiting", None) when the error just means no internet, else ("failed", the useful line)."""
    if OFFLINE.search(stderr or ""):
        return "waiting", None
    lines = [x.strip() for x in (stderr or "").splitlines()
             if x.strip() and "could not read from remote repository" not in x.lower()]
    return "failed", (lines[0] if lines else "GitHub refused")[:200]


def reach(logs, env):
    """Does GitHub answer? ("ok", None), ("waiting", None) or ("failed", why)."""
    try:
        r = git(logs, "ls-remote", "--heads", "origin", env=env, timeout=20, check=False)
    except SyncError:
        return "waiting", None
    return ("ok", None) if r.returncode == 0 else classify(r.stderr)


def fingerprint(key_line):
    """SHA256 fingerprint of an OpenSSH public key line, as ssh-keygen -l prints it."""
    blob = base64.b64decode(key_line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def current_drive(status_file=STATUS_FILE, now=time.time):
    """The drive being written ("2026-10-30/drive-081502.csv"), from the logger's status file."""
    data = carpi_presets.read_json(status_file)
    if (isinstance(data, dict) and data.get("state") == "logging" and isinstance(data.get("drive"), str)
            and now() - data.get("updated", 0) <= STATUS_FRESH_S):
        return data["drive"]
    return None


def adopt(logs, remote, env):
    """Make logs a checkout of remote without touching the drive files already in it."""
    logs = Path(logs)
    logs.mkdir(parents=True, exist_ok=True)
    git(logs, "init", "-q", "-b", "main", env=env)
    if git(logs, "remote", "get-url", "origin", env=env, check=False).returncode:
        git(logs, "remote", "add", "origin", remote, env=env)
    else:
        git(logs, "remote", "set-url", "origin", remote, env=env)
    if git(logs, "ls-remote", "--heads", "origin", "main", env=env, timeout=30).stdout.strip():
        git(logs, "fetch", "-q", "--depth", "50", "origin", "main", env=env, timeout=300)
        git(logs, "reset", "-q", "--mixed", "FETCH_HEAD", env=env)
        tracked = set(git(logs, "ls-tree", "--name-only", "HEAD", env=env).stdout.split())
        own = [f for f in (".gitignore", "README.md", ".github") if f in tracked]
        if own:                                           # the repo's own files, as GitHub has them
            git(logs, "checkout", "-q", "HEAD", "--", *own, env=env)
    if not (logs / ".gitignore").exists():
        (logs / ".gitignore").write_text(GITIGNORE)


def repair(logs, remote, env):
    """Undo what a power cut mid-git can leave behind. Returns notes for the journal."""
    notes = []
    lock = logs / ".git" / "index.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_S:
        lock.unlink()
        notes.append("removed a stale index.lock")
    if git(logs, "status", "--porcelain", env=env, check=False).returncode:
        broken = logs / f".git.broken-{datetime.now():%Y%m%d-%H%M%S}"
        (logs / ".git").rename(broken)
        adopt(logs, remote, env)
        notes.append(f"the checkout was damaged: kept it as {broken.name} and started over")
    return notes


def pending(logs, env, current=None):
    """Drives (by name) with files not on GitHub yet, leaving out the one being written."""
    skip = DRIVE_FILE.fullmatch(current).group(1) if current and DRIVE_FILE.fullmatch(current) else None
    out = git(logs, "status", "--porcelain", "-uall", env=env).stdout
    drives = set()
    for line in out.splitlines():
        m = DRIVE_FILE.fullmatch(line[3:].strip('"'))
        if m and m.group(1) != skip:
            drives.add(m.group(1))
    return drives


def push(logs, env):
    r = git(logs, "push", "-q", "origin", "main", env=env, timeout=300, check=False)
    if r.returncode == 0:
        return
    if not re.search(r"rejected|fetch first|non-fast-forward", r.stderr):
        raise SyncError(f"git push: {classify(r.stderr)[1] or 'no internet'}")
    # GitHub has commits the Pi doesn't (say, a .json edited to hide a drive): put ours on top
    git(logs, "fetch", "-q", "--depth", "50", "origin", "main", env=env, timeout=300)
    if git(logs, *AUTHOR, "rebase", "-q", "FETCH_HEAD", env=env, check=False).returncode:
        git(logs, "rebase", "--abort", env=env, check=False)
        raise SyncError("couldn't combine with newer changes on GitHub")
    git(logs, "push", "-q", "origin", "main", env=env, timeout=300)


def sync_once(logs, remote, env, current=None, check_online=reach):
    """One upload. Returns {state: ok|waiting|failed, uploaded, waiting, error}."""
    logs = Path(logs)
    if not (logs / ".git").is_dir():
        return {"state": "failed", "uploaded": 0, "waiting": None, "error": SETUP_HINT}
    notes = repair(logs, remote, env)
    for note in notes:
        print(note, flush=True)
    state, error = check_online(logs, env)
    if state != "ok":
        return {"state": state, "uploaded": 0, "waiting": len(pending(logs, env, current)), "error": error}
    git(logs, "add", "-A", env=env)
    if current:
        stem = current[:-len(".csv")] if current.endswith(".csv") else current
        git(logs, "reset", "-q", "--", f"{stem}.csv", f"{stem}.json", env=env, check=False)
    staged = git(logs, "diff", "--cached", "--name-only", env=env).stdout.split()
    uploaded = {m.group(1) for m in map(DRIVE_FILE.fullmatch, staged) if m}
    if staged:
        git(logs, *AUTHOR, "commit", "-q", "-m", f"{len(uploaded)} drive(s) from the Pi", env=env)
        push(logs, env)
    return {"state": "ok", "uploaded": len(uploaded), "waiting": len(pending(logs, env, current)), "error": None}


def run(logs=LOGS, remote=REMOTE, env=None, settings=SETTINGS, sync_file=SYNC_FILE, trigger=TRIGGER,
        status_file=STATUS_FILE, manual=False, check_online=reach, up=uptime):
    """What the timer (or Upload now) runs: the settings, one upload, and sync.json."""
    logs = Path(logs)
    if Path(trigger).exists():                     # Upload now from the phone
        manual = True
        try:
            Path(trigger).unlink()
        except FileNotFoundError:
            pass
    env = ssh_env() if env is None else env
    current = current_drive(status_file)
    if not manual and not read_settings(settings)["auto"]:
        try:
            waiting = len(pending(logs, env, current)) if (logs / ".git").is_dir() else None
        except SyncError:
            waiting = None
        write_status(sync_file, state="off", waiting=waiting, error=None)
        return {"state": "off", "uploaded": 0, "waiting": waiting, "error": None}
    write_status(sync_file, state="uploading", error=None, attempt_up=round(up(), 1),
                 last_attempt=datetime.now().isoformat(timespec="seconds"))
    try:
        result = sync_once(logs, remote, env, current, check_online)
    except (SyncError, OSError) as e:
        result = {"state": "failed", "uploaded": 0, "waiting": None, "error": str(e)[:200]}
    fields = {"state": result["state"], "error": result["error"], "uploaded": result["uploaded"]}
    if result["waiting"] is not None:
        fields["waiting"] = result["waiting"]
    if result["state"] == "ok":
        fields["success_up"] = round(up(), 1)
    write_status(sync_file, **fields)
    return result


class Uploads:
    """The phone page's side of uploads: carpi_control's backend for "upload" and "upload_now"."""

    def __init__(self, settings=SETTINGS, logs=LOGS, sync_file=SYNC_FILE, trigger=TRIGGER, up=uptime):
        self.settings = Path(settings)
        self.logs = Path(logs)
        self.sync_file = Path(sync_file)
        self.trigger = Path(trigger)
        self.up = up

    def status(self):
        data = carpi_presets.read_json(self.sync_file)
        data = data if isinstance(data, dict) else {}
        now = self.up()

        def age(key):
            t = data.get(key)
            return round(now - t, 1) if isinstance(t, (int, float)) and not isinstance(t, bool) and t <= now else None
        settings = read_settings(self.settings)
        state, error, attempt = data.get("state"), data.get("error"), age("attempt_up")
        if state == "uploading" and attempt is not None and attempt > STUCK_S:
            state, error = "failed", "the upload stopped unexpectedly"
        if not settings["auto"]:
            next_try = None
        elif attempt is None:
            next_try = max(0, round(FIRST_S - now))
        else:
            next_try = max(0, round(EVERY_S - attempt))
        return {"set_up": (self.logs / ".git").is_dir(), **settings, "state": state,
                "waiting": data.get("waiting"), "error": error, "last_success_age_s": age("success_up"),
                "last_attempt_age_s": attempt, "next_try_s": next_try}

    def change(self, **changes):
        write_settings(self.settings, **changes)
        return self.status()

    def start_now(self):
        if not (self.logs / ".git").is_dir():
            raise ValueError("Uploads aren't set up on this Pi yet")
        before = carpi_presets.read_json(self.sync_file)
        before = before if isinstance(before, dict) else {}
        # "uploading" first: the upload itself may finish before we'd get to write it
        write_status(self.sync_file, state="uploading", error=None, attempt_up=round(self.up(), 1),
                     last_attempt=datetime.now().isoformat(timespec="seconds"))
        try:
            self.trigger.write_text("")
        except OSError as e:
            write_status(self.sync_file, **{k: before.get(k) for k in ("state", "error", "attempt_up", "last_attempt")})
            raise ValueError(f"couldn't start the upload: {e.strerror or e}") from None
        return dict(self.status(), started=True)


# ---- one-time setup ----

def keygen(key=KEY, known_hosts=KNOWN_HOSTS):
    """The Pi's own key for carpi-data, and GitHub's host key pinned from the constant above."""
    if fingerprint(GITHUB_ED25519) != GITHUB_FINGERPRINT:
        sys.exit("The built-in GitHub host key doesn't match its fingerprint; not writing it.")
    key.parent.mkdir(mode=0o700, exist_ok=True)
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "carpi-pi upload",
                        "-f", str(key)], check=True)
    known_hosts.write_text(f"github.com {GITHUB_ED25519}\n")
    print("Add this as a deploy key with write access on GitHub (carpi-data > Settings > Deploy keys):\n")
    print(Path(f"{key}.pub").read_text().strip())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--now", action="store_true", help="upload even with automatic uploads off")
    mode.add_argument("--setup", action="store_true", help="adopt the logs folder and upload everything")
    mode.add_argument("--keygen", action="store_true", help="make the upload key and pin GitHub's host key")
    ap.add_argument("--logs", type=Path, default=LOGS)
    ap.add_argument("--remote", default=REMOTE)
    ap.add_argument("--settings", type=Path, default=SETTINGS)
    ap.add_argument("--sync-file", type=Path, default=SYNC_FILE)
    ap.add_argument("--status-file", type=Path, default=STATUS_FILE)
    a = ap.parse_args(argv)
    if a.keygen:
        keygen()
        return 0
    env = ssh_env()
    if a.setup:
        print(f"Adopting {a.logs} as a checkout of {a.remote} ...", flush=True)
        try:
            adopt(a.logs, a.remote, env)
        except SyncError as e:
            sys.exit(f"Setup failed: {e}")
    result = run(a.logs, a.remote, env, a.settings, a.sync_file, TRIGGER, a.status_file,
                 manual=a.now or a.setup)
    if a.now or a.setup or result["uploaded"] or result["state"] == "failed":
        print(json.dumps(result), flush=True)
    return 1 if (a.now or a.setup) and result["state"] != "ok" else 0


if __name__ == "__main__":
    sys.exit(main())
