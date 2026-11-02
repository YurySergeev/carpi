"""carpi_admin: shut down / restart and the phone-set clock, with every system call faked."""
import pytest

import carpi_admin as ca

PI_NOW = 1_793_000_000.0          # what the Pi believes it is


class Rig:
    def __init__(self, tmp_path, synced=False):
        self.synced = tmp_path / "synchronized"
        if synced:
            self.synced.write_text("")
        self.marker = tmp_path / "clock_from_phone"
        self.set_to = []
        self.ran = []

    def admin(self, set_time=None):
        return ca.Admin(run=lambda cmd, **kw: self.ran.append(cmd), now=lambda: PI_NOW,
                        set_time=set_time or self.set_to.append, synced=self.synced, marker=self.marker)


def test_status_sources(tmp_path):
    r = Rig(tmp_path)
    assert r.admin().clock_status() == {"synced": False, "source": "none", "pi_time_ms": int(PI_NOW * 1000)}
    r.marker.write_text("")
    assert r.admin().clock_status()["source"] == "phone"
    r.synced.write_text("")                       # internet sync wins over the phone
    assert r.admin().clock_status() == {"synced": True, "source": "ntp", "pi_time_ms": int(PI_NOW * 1000)}


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
