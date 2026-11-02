"""carpi_net: Wi-Fi through NetworkManager, against a fake nmcli. Network names are made up."""
import json

import pytest

import carpi_net as cn

HOME = "11111111-2222-3333-4444-555555555555"
SPOT = "66666666-7777-8888-9999-aaaaaaaaaaaa"
PASSWORD = "hunter22"

CONNECTIONS = (f"HomeNet:{HOME}:802-11-wireless:10:yes\n"
               f"PhoneSpot:{SPOT}:802-11-wireless:5:no\n"
               "Wired connection 1:abcdefab-0000-0000-0000-000000000000:802-3-ethernet:-999:no\n"
               "lo:0f0f0f0f-0000-0000-0000-000000000000:loopback:0:yes\n")
WIFI_LIST = ("*:HomeNet:72:WPA2\n"
             " :PhoneSpot:55:WPA2 WPA3\n"
             " :Cafe\\: Guest:40:\n"
             " ::30:WPA2\n"                      # hidden network: no name
             " :Neighbor 5G:61:WPA2\n"
             " :Cafe\\: Guest:47:\n")             # the same network seen twice: keep the stronger
DEVICES = "wlan0:wifi:connected:HomeNet\neth0:ethernet:unavailable:\nlo:loopback:connected (externally):lo\n"


class FakeNm:
    def __init__(self, fail=None):
        self.calls = []
        self.added = []
        self.activated = []
        self.fail = fail

    def run(self, args):
        self.calls.append(list(args))
        if self.fail:
            raise cn.NetError(self.fail)
        if args[:4] == ["-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY,ACTIVE", "connection"]:
            return CONNECTIONS
        if args[:3] == ["-t", "-g", "802-11-wireless.ssid"]:
            return {HOME: "HomeNet\n", SPOT: "PhoneSpot\n"}[args[-1]]
        if args[:4] == ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device"]:
            return WIFI_LIST
        if args[:4] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            return DEVICES
        return ""

    def add_connection(self, settings):
        self.added.append(settings)
        return "/org/freedesktop/NetworkManager/Settings/9"

    def activate(self, path):
        self.activated.append(path)


@pytest.fixture
def nm():
    return FakeNm()


@pytest.fixture
def net(nm):
    return cn.Net(run=nm.run, add_connection=nm.add_connection, activate=nm.activate,
                  addresses=lambda: {"wlan0": ["192.168.1.40"]}, hostname=lambda: "carpi")


def test_split_terse_handles_escapes():
    assert cn.split_terse("Cafe\\: Guest:72:WPA2") == ["Cafe: Guest", "72", "WPA2"]
    assert cn.split_terse("a\\\\b::c") == ["a\\b", "", "c"]
    assert cn.split_terse("") == [""]


def test_state(net):
    s = net.state()
    assert s["hostname"] == "carpi" and s["addresses"] == {"wlan0": ["192.168.1.40"]}
    assert s["wifi"] == {"state": "connected", "ssid": "HomeNet", "signal": 72, "ip": "192.168.1.40"}
    assert [(n["ssid"], n["priority"], n["active"]) for n in s["saved"]] == [("HomeNet", 10, True), ("PhoneSpot", 5, False)]
    assert all(set(n) == {"uuid", "name", "ssid", "priority", "active"} for n in s["saved"])


def test_scan_drops_hidden_merges_duplicates_and_sorts(net, nm):
    nearby = net.scan()
    assert ["device", "wifi", "rescan"] in nm.calls
    assert [(n["ssid"], n["signal"], n["secured"], n["saved"]) for n in nearby] == [
        ("HomeNet", 72, True, True), ("Neighbor 5G", 61, True, False),
        ("PhoneSpot", 55, True, True), ("Cafe: Guest", 47, False, False)]


def test_scan_without_rescan_only_reads(net, nm):
    net.scan(rescan=False)
    assert ["device", "wifi", "rescan"] not in nm.calls


def test_rescan_already_running_is_not_an_error(nm):
    def run(args):
        if args == ["device", "wifi", "rescan"]:
            raise cn.NetError("Scanning not allowed while already scanning")
        return nm.run(args)
    n = cn.Net(run=run, add_connection=nm.add_connection, activate=nm.activate,
               addresses=lambda: {}, hostname=lambda: "carpi")
    assert len(n.scan()) == 4


def test_set_order(net, nm):
    net.set_order([SPOT, HOME])
    mods = [c for c in nm.calls if c[:2] == ["connection", "modify"]]
    assert mods == [["connection", "modify", "uuid", SPOT, "connection.autoconnect-priority", "10"],
                    ["connection", "modify", "uuid", HOME, "connection.autoconnect-priority", "9"]]


def test_connect_returns_at_once_and_forget_deletes(net, nm):
    net.connect(SPOT)
    net.forget(SPOT)
    assert ["--wait", "0", "connection", "up", "uuid", SPOT] in nm.calls
    assert ["connection", "delete", "uuid", SPOT] in nm.calls


@pytest.mark.parametrize("bad", ["", "nope", "../../etc", HOME + "; reboot",
                                 "ABCDEFAB-2222-3333-4444-555555555555"])      # NetworkManager ids are lowercase
def test_bad_uuid_is_refused_before_nmcli(net, nm, bad):
    for call in (net.connect, net.forget):
        with pytest.raises(cn.NetError, match="network id"):
            call(bad)
    with pytest.raises(cn.NetError, match="network id"):
        net.set_order([HOME, bad])
    assert nm.calls == []


@pytest.mark.parametrize("ssid,password,ok", [
    ("HomeNet", PASSWORD, True), ("Cafe Guest", "", True), ("x" * 32, "12345678", True),
    ("", PASSWORD, False), ("x" * 33, PASSWORD, False), ("é" * 17, PASSWORD, False),   # 34 bytes
    ("HomeNet", "1234567", False), ("HomeNet", "x" * 64, False),
])
def test_validate(ssid, password, ok):
    if ok:
        cn.validate(ssid, password)
    else:
        with pytest.raises(cn.NetError):
            cn.validate(ssid, password)


def test_add_secured_network_at_lowest_priority_and_connect(net, nm):
    out = net.add("Cafe Guest WPA", PASSWORD, connect=True)
    [s] = nm.added
    assert s["802-11-wireless"]["ssid"] == b"Cafe Guest WPA"
    assert s["802-11-wireless-security"] == {"key-mgmt": "wpa-psk", "psk": PASSWORD}
    assert s["connection"]["autoconnect-priority"] == 4          # below PhoneSpot's 5
    assert s["connection"]["autoconnect-retries"] == 0
    assert nm.activated == ["/org/freedesktop/NetworkManager/Settings/9"]
    assert out == {"added": "Cafe Guest WPA", "connecting": True}


def test_add_open_network_has_no_security_and_does_not_connect(net, nm):
    net.add("Cafe Guest", "", connect=False)
    [s] = nm.added
    assert "802-11-wireless-security" not in s and nm.activated == []


def test_add_refuses_a_network_that_is_already_saved(net, nm):
    with pytest.raises(cn.NetError, match="already saved"):
        net.add("HomeNet", PASSWORD)
    assert nm.added == []


def test_password_never_leaves_the_add_call(net, nm):
    out = net.add("Cafe Guest WPA", PASSWORD, connect=True)
    shown = json.dumps([nm.calls, out, net.state(), net.scan()])
    assert PASSWORD not in shown


@pytest.mark.parametrize("stderr", ["Error: Not authorized to control networking.",
                                    "Error: insufficient privileges."])
def test_permission_errors_point_to_the_setup(stderr):
    assert cn.friendly(stderr) == cn.SETUP_HINT


def test_other_errors_are_short():
    msg = cn.friendly("Error: Connection activation failed: Secrets were required.\nmore detail\n" + "x" * 500)
    assert msg.startswith("Connection activation failed") and len(msg) <= 160


def test_addresses_from_ip_json():
    raw = json.dumps([{"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
                      {"ifname": "wlan0", "addr_info": [{"local": "192.168.1.40"}]},
                      {"ifname": "eth0", "addr_info": []}])
    assert cn.parse_addresses(raw) == {"wlan0": ["192.168.1.40"]}
