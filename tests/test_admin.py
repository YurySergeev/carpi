"""carpi_admin: shut down / restart, the phone-set clock and time zone, with every system call faked."""
import subprocess

import pytest

import carpi_admin as ca

PI_NOW = 1_793_000_000.0          # what the Pi believes it is


class Rig:
    def __init__(self, tmp_path, synced=False, zone="UTC", tz_fails=False, set_error=None):
        self.synced = tmp_path / "synchronized"
        if synced:
            self.synced.write_text("")
        self.marker = tmp_path / "clock_from_phone"
        self.set_to = []
        self.ran = []
        self.zone, self.tz_fails, self.set_error = zone, tz_fails, set_error
        self.tz_calls = []
        self.zoneinfo = tmp_path / "zoneinfo"
        for name in ("UTC", "America/New_York", "Europe/Berlin"):
            (self.zoneinfo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.zoneinfo / name).write_text("TZif")

    def run(self, cmd, **kw):
        """A fake timedatectl; anything else (power) is only recorded."""
        if cmd[0] != "timedatectl":
            self.ran.append(cmd)
            return None
        self.tz_calls.append(cmd)
        if cmd[1:] == ["show", "-p", "Timezone", "--value"]:
            if self.tz_fails:
                return subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus")
            return subprocess.CompletedProcess(cmd, 0, self.zone + "\n", "")
        if self.set_error:
            return subprocess.CompletedProcess(cmd, 1, "", self.set_error)
        self.zone = cmd[-1]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def admin(self, set_time=None):
        return ca.Admin(run=self.run, now=lambda: PI_NOW, set_time=set_time or self.set_to.append,
                        synced=self.synced, marker=self.marker, zoneinfo=self.zoneinfo)


def test_status_sources(tmp_path):
    r = Rig(tmp_path)
    assert r.admin().clock_status() == {"synced": False, "source": "none", "pi_time_ms": int(PI_NOW * 1000),
                                        "zone": "UTC"}
    r.marker.write_text("")
    assert r.admin().clock_status()["source"] == "phone"
    r.synced.write_text("")                       # internet sync wins over the phone
    assert r.admin().clock_status() == {"synced": True, "source": "ntp", "pi_time_ms": int(PI_NOW * 1000),
                                        "zone": "UTC"}


def test_phone_ahead_sets_the_clock_and_writes_the_marker(tmp_path):
    r = Rig(tmp_path)
    out = r.admin().set_clock((PI_NOW + 3 * 86400) * 1000)
    assert out["set"] and out["source"] == "phone"
    assert r.set_to == [PI_NOW + 3 * 86400] and r.marker.exists()


def test_synced_clock_is_left_alone(tmp_path):
    r = Rig(tmp_path, synced=True)
    out = r.admin().set_clock((PI_NOW + 600) * 1000)
    assert not out["set"] and "internet" in out["reason"] and r.set_to == []


@pytest.mark.parametrize("offset_s", [-3600, 0, 1.5])
def test_phone_behind_or_equal_is_refused(tmp_path, offset_s):
    r = Rig(tmp_path)
    out = r.admin().set_clock((PI_NOW + offset_s) * 1000)
    assert not out["set"] and "ahead" in out["reason"] and r.set_to == [] and not r.marker.exists()


def test_absurd_phone_time_is_refused(tmp_path):
    r = Rig(tmp_path)
    out = r.admin().set_clock((PI_NOW + 2 * 365 * 86400) * 1000)
    assert not out["set"] and "wrong" in out["reason"] and r.set_to == []


@pytest.mark.parametrize("bad", [None, "soon", float("nan"), -5])
def test_bad_phone_time_is_an_error(tmp_path, bad):
    with pytest.raises(ValueError):
        Rig(tmp_path).admin().set_clock(bad)


def test_missing_permission_points_to_the_setup(tmp_path):
    def denied(t):
        raise PermissionError(1, "Operation not permitted")
    r = Rig(tmp_path)
    with pytest.raises(ValueError, match="pi-setup/install.sh"):
        r.admin(set_time=denied).set_clock((PI_NOW + 600) * 1000)
    assert not r.marker.exists()


def test_power_returns_the_action_and_runs_nothing_yet(tmp_path):
    r = Rig(tmp_path)
    admin = r.admin()
    off = admin.power("shutdown")
    again = admin.power("reboot")
    assert r.ran == []
    off()
    again()
    assert r.ran == [["systemctl", "poweroff"], ["systemctl", "reboot"]]


@pytest.mark.parametrize("bad", ["format", "", None, "halt; rm -rf /"])
def test_unknown_power_action(tmp_path, bad):
    with pytest.raises(ValueError):
        Rig(tmp_path).admin().power(bad)


def test_reads_the_zone(tmp_path):
    assert Rig(tmp_path, zone="Europe/Berlin").admin().time_zone() == "Europe/Berlin"
    assert Rig(tmp_path, tz_fails=True).admin().time_zone() is None


def test_sets_a_new_zone(tmp_path):
    r = Rig(tmp_path)
    out = r.admin().set_zone("America/New_York")
    assert out == {"zone": "America/New_York", "set": True, "reason": "set from the phone"}
    assert r.tz_calls[-1] == ["timedatectl", "--no-ask-password", "set-timezone", "America/New_York"]
    assert r.zone == "America/New_York"


def test_same_zone_changes_nothing(tmp_path):
    r = Rig(tmp_path, zone="America/New_York")
    out = r.admin().set_zone("America/New_York")
    assert not out["set"] and out["reason"] == "already this time zone"
    assert all("set-timezone" not in c for c in r.tz_calls)


@pytest.mark.parametrize("bad", [None, 5, "", "../../etc/passwd", "/etc/localtime", "America/",
                                 "Ameri ca/X", "a" * 65])
def test_bad_zone_names_are_refused(tmp_path, bad):
    r = Rig(tmp_path)
    with pytest.raises(ValueError, match="time zone name"):
        r.admin().set_zone(bad)
    assert all("set-timezone" not in c for c in r.tz_calls)


@pytest.mark.parametrize("zone", ["Mars/Olympus", "America"])      # no such file; a folder
def test_unknown_zone_is_refused(tmp_path, zone):
    with pytest.raises(ValueError, match="unknown time zone"):
        Rig(tmp_path).admin().set_zone(zone)


def test_zone_permission_missing_points_to_the_setup(tmp_path):
    r = Rig(tmp_path, set_error="Failed to set time zone: Access denied")
    with pytest.raises(ValueError, match="pi-setup/install.sh"):
        r.admin().set_zone("Europe/Berlin")
