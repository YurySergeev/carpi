"""carpi_sync: uploads from the Pi, with real git and a local bare repo standing in for GitHub."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import carpi_sync as cs

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "2026-09-26"
OFFLINE = lambda logs, env: ("waiting", None)          # noqa: E731


class Rig:
    def __init__(self, tmp):
        self.tmp = tmp
        (tmp / "gitconfig").write_text("")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                        GIT_TERMINAL_PROMPT="0")
        self.remote = tmp / "remote.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote))
        self.logs = tmp / "logs"
        self.logs.mkdir()
        self.settings = tmp / "upload.json"
        self.sync_file = tmp / "sync.json"
        self.trigger = tmp / "sync-now"
        self.status_file = tmp / "status.json"
        self.up = 1000.0

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], capture_output=True, text=True, env=self.env, cwd=cwd, check=True)

    def add_drive(self, day, name, src="drive-231349"):
        folder = self.logs / day
        folder.mkdir(parents=True, exist_ok=True)
        for ext in (".csv", ".json"):
            shutil.copy(SAMPLE / f"{src}{ext}", folder / f"{name}{ext}")

    def remote_files(self):
        r = subprocess.run(["git", "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main"],
                           capture_output=True, text=True, env=self.env)
        return set(r.stdout.split()) if r.returncode == 0 else set()

    def commits(self):
        r = subprocess.run(["git", "--git-dir", str(self.remote), "rev-list", "--count", "main"],
                           capture_output=True, text=True, env=self.env)
        return int(r.stdout) if r.returncode == 0 else 0

    def logging(self, drive):
        self.status_file.write_text(json.dumps({"state": "logging", "drive": drive, "updated": time.time()}))

    def run(self, **kw):
        kw.setdefault("check_online", cs.reach)
        return cs.run(logs=self.logs, remote=str(self.remote), env=self.env, settings=self.settings,
                      sync_file=self.sync_file, trigger=self.trigger, status_file=self.status_file,
                      up=lambda: self.up, **kw)

    def setup(self):
        cs.adopt(self.logs, str(self.remote), self.env)
        return self.run(manual=True)

    def sync(self):
        return json.loads(self.sync_file.read_text())

    def uploads(self):
        return cs.Uploads(settings=self.settings, logs=self.logs, sync_file=self.sync_file,
                          trigger=self.trigger, up=lambda: self.up)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


DRIVE_A = {"2026-09-26/drive-223818.csv", "2026-09-26/drive-223818.json"}
DRIVE_B = {"2026-09-26/drive-231349.csv", "2026-09-26/drive-231349.json"}


def test_setup_uploads_every_drive(rig):
    rig.add_drive("2026-09-26", "drive-223818", "drive-223818")
    rig.add_drive("2026-09-26", "drive-231349")
    result = rig.setup()
    assert result["state"] == "ok" and result["uploaded"] == 2 and result["waiting"] == 0
    assert rig.remote_files() == DRIVE_A | DRIVE_B | {".gitignore"}
    s = rig.sync()
    assert s["state"] == "ok" and s["success_up"] == 1000.0 and s["attempt_up"] == 1000.0


def test_the_drive_being_logged_stays_behind(rig):
    rig.setup()
    rig.add_drive("2026-09-26", "drive-223818", "drive-223818")
    rig.add_drive("2026-10-30", "drive-081502")
    rig.logging("2026-10-30/drive-081502.csv")
    result = rig.run()
    assert result["state"] == "ok" and result["uploaded"] == 1
    assert result["waiting"] == 0                          # the drive being written isn't "waiting"
    assert DRIVE_A <= rig.remote_files()
    assert not any("081502" in f for f in rig.remote_files())
    rig.status_file.write_text(json.dumps({"state": "waiting", "updated": time.time()}))
    assert rig.run()["uploaded"] == 1                      # it goes up once it's finished
    assert "2026-10-30/drive-081502.csv" in rig.remote_files()


def test_tmp_files_are_never_uploaded(rig):
    rig.add_drive("2026-09-26", "drive-231349")
    (rig.logs / "2026-09-26" / "drive-231349.json.tmp").write_text("{")
    rig.setup()
    assert not any(f.endswith(".tmp") for f in rig.remote_files())


def test_nothing_new_makes_no_commit(rig):
    rig.add_drive("2026-09-26", "drive-231349")
    rig.setup()
    before = rig.commits()
    result = rig.run()
    assert result["state"] == "ok" and result["uploaded"] == 0 and rig.commits() == before


def test_no_internet_waits(rig):
    rig.setup()
    rig.add_drive("2026-09-26", "drive-231349")
    result = rig.run(check_online=OFFLINE)
    assert result["state"] == "waiting" and result["waiting"] == 1
    assert rig.sync()["state"] == "waiting" and DRIVE_B.isdisjoint(rig.remote_files())


@pytest.mark.parametrize("stderr, state", [
    ("ssh: Could not resolve hostname github.com: Temporary failure in name resolution\n"
     "fatal: Could not read from remote repository.", "waiting"),
    ("ssh: connect to host github.com port 22: Network is unreachable", "waiting"),
    ("ssh: connect to host github.com port 22: Connection timed out", "waiting"),
    ("git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.", "failed"),
    ("Host key verification failed.\nfatal: Could not read from remote repository.", "failed"),
    ("ERROR: Repository not found.\nfatal: Could not read from remote repository.", "failed"),
])
def test_offline_is_told_apart_from_a_real_problem(stderr, state):
    got, error = cs.classify(stderr)
    assert got == state
    assert (error is None) == (state == "waiting")
    if error:
        assert "Could not read" not in error                # the useful line, not git's generic one


def test_a_wrong_remote_fails_instead_of_waiting(rig):
    rig.setup()
    state, error = cs.reach(rig.logs, rig.env)
    assert state == "ok"
    rig.git("-C", str(rig.logs), "remote", "set-url", "origin", str(rig.tmp / "nowhere.git"))
    state, error = cs.reach(rig.logs, rig.env)
    assert state == "failed" and error


def test_stale_lock_is_cleared_but_a_fresh_one_is_left(rig):
    rig.setup()
    rig.add_drive("2026-09-26", "drive-231349")
    lock = rig.logs / ".git" / "index.lock"
    lock.write_text("")
    assert rig.run()["state"] == "failed" and lock.exists()     # maybe another git is running
    old = time.time() - 300
    os.utime(lock, (old, old))
    assert rig.run()["state"] == "ok" and not lock.exists()
    assert DRIVE_B <= rig.remote_files()


def test_a_damaged_checkout_is_rebuilt_without_losing_drives(rig):
    rig.add_drive("2026-09-26", "drive-231349")
    rig.setup()
    (rig.logs / ".git" / "HEAD").unlink()
    rig.add_drive("2026-09-26", "drive-223818", "drive-223818")
    result = rig.run()
    assert result["state"] == "ok"
    assert any(p.name.startswith(".git.broken-") for p in rig.logs.iterdir())
    assert (rig.logs / "2026-09-26" / "drive-231349.csv").exists()
    assert rig.remote_files() == DRIVE_A | DRIVE_B | {".gitignore"}      # nothing from .git.broken-*


def test_newer_changes_on_github_are_combined(rig):
    rig.add_drive("2026-09-26", "drive-231349")
    rig.setup()
    other = rig.tmp / "other"
    rig.git("clone", "-q", str(rig.remote), str(other))
    (other / "README.md").write_text("carpi-data\n")
    rig.git("-C", str(other), "add", "README.md")
    rig.git("-C", str(other), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "readme")
    rig.git("-C", str(other), "push", "-q", "origin", "main")
    rig.add_drive("2026-09-26", "drive-223818", "drive-223818")
    assert rig.run()["state"] == "ok"
    assert {"README.md"} | DRIVE_A | DRIVE_B <= rig.remote_files()


def test_status_keeps_the_last_success_through_a_failure(rig):
    rig.add_drive("2026-09-26", "drive-231349")
    rig.setup()
    rig.up = 1100.0
    rig.add_drive("2026-09-26", "drive-223818", "drive-223818")
    rig.run(check_online=OFFLINE)
    s = rig.sync()
    assert s["state"] == "waiting" and s["success_up"] == 1000.0 and s["attempt_up"] == 1100.0


def test_not_set_up_says_so(rig):
    rig.add_drive("2026-09-26", "drive-231349")
    result = rig.run()
    assert result["state"] == "failed" and "--setup" in result["error"]


def test_github_fingerprint_matches_the_built_in_key():
    assert cs.fingerprint(cs.GITHUB_ED25519) == cs.GITHUB_FINGERPRINT


# ---- settings from the phone page ----

def test_automatic_off_stays_offline(rig):
    rig.setup()
    rig.add_drive("2026-09-26", "drive-231349")
    cs.write_settings(rig.settings, auto=False)

    def never(logs, env):
        raise AssertionError("went online with automatic uploads off")
    result = rig.run(check_online=never)
    assert result["state"] == "off" and result["waiting"] == 1
    assert rig.sync()["state"] == "off" and DRIVE_B.isdisjoint(rig.remote_files())


def test_upload_now_runs_even_with_automatic_off(rig):
    rig.setup()
    rig.add_drive("2026-09-26", "drive-231349")
    cs.write_settings(rig.settings, auto=False)
    rig.trigger.write_text("")
    assert rig.run()["state"] == "ok"
    assert not rig.trigger.exists() and DRIVE_B <= rig.remote_files()


def test_uploading_is_reported_before_going_online(rig):
    rig.setup()
    seen = []

    def spy(logs, env):
        seen.append(rig.sync()["state"])
        return "waiting", None
    rig.run(check_online=spy)
    assert seen == ["uploading"]


@pytest.mark.parametrize("text, expected", [
    (None, {"auto": True, "publish": True}),                      # no file: today's behaviour
    ('{"auto": false}', {"auto": False, "publish": True}),
    ('{"auto": true, "publish": false}', {"auto": True, "publish": False}),
    ('{"publish": "no"}', {"auto": True, "publish": False}),       # not a boolean: private
    ("{broken", {"auto": True, "publish": False}),                # unreadable: private
    ("[1]", {"auto": True, "publish": False}),
])
def test_read_settings(tmp_path, text, expected):
    path = tmp_path / "upload.json"
    if text is not None:
        path.write_text(text)
    assert cs.read_settings(path) == expected


def test_write_settings_merges_and_checks(tmp_path):
    path = tmp_path / "upload.json"
    assert cs.write_settings(path, publish=False) == {"auto": True, "publish": False}
    assert cs.write_settings(path, auto=False) == {"auto": False, "publish": False}
    assert json.loads(path.read_text()) == {"auto": False, "publish": False}
    for bad in ({}, {"auto": "yes"}, {"publish": 1}, {"color": True}):
        with pytest.raises(ValueError):
            cs.write_settings(path, **bad)
    assert cs.read_settings(path) == {"auto": False, "publish": False}


def test_uploads_status_for_the_phone(rig):
    u = rig.uploads()
    s = u.status()
    assert s["set_up"] is False and s["auto"] and s["publish"] and s["state"] is None
    rig.up = 20.0
    assert u.status()["next_try_s"] == 40                          # first run 60 s after boot
    rig.up = 1000.0
    rig.setup()
    rig.up = 1030.0
    s = u.status()
    assert s["set_up"] and s["state"] == "ok" and s["last_success_age_s"] == 30.0
    assert s["last_attempt_age_s"] == 30.0 and s["next_try_s"] == 90
    cs.write_settings(rig.settings, auto=False)
    assert u.status()["next_try_s"] is None


def test_a_stuck_upload_shows_as_failed(rig):
    cs.write_status(rig.sync_file, state="uploading", attempt_up=100.0)
    rig.up = 100.0 + cs.STUCK_S + 1
    s = rig.uploads().status()
    assert s["state"] == "failed" and "stopped" in s["error"]


def test_upload_now_from_the_phone(rig):
    u = rig.uploads()
    with pytest.raises(ValueError, match="set up"):
        u.start_now()
    assert not rig.trigger.exists()
    rig.setup()
    s = u.start_now()
    assert s["started"] and s["state"] == "uploading" and rig.trigger.exists()


def test_upload_now_failure_puts_the_status_back(rig):
    rig.setup()
    rig.trigger = rig.tmp / "missing-folder" / "sync-now"
    with pytest.raises(ValueError, match="couldn't start"):
        rig.uploads().start_now()
    assert rig.sync()["state"] == "ok"
