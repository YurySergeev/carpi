import json

import carpi_system as cs

IP_JSON = json.dumps([{"ifname": "can0", "linkinfo": {"info_kind": "can", "info_data": {
    "state": "ERROR-ACTIVE", "berr_counter": {"tx": 0, "rx": 0}, "bittiming": {"bitrate": 500000}}}}])


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def fake_pi(root, rx=1000, cpu=(100, 50, 200, 1000, 50), can=True, flags="0x40081"):
    """A minimal /proc and /sys tree like a Pi 4's."""
    def put(rel, text):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    put("proc/stat", "cpu  %d %d %d %d %d 0 0 0 0 0\ncpu0 1 2 3 4 5 0 0 0 0 0\n" % cpu)
    put("proc/meminfo", "MemTotal:        1900000 kB\nMemFree:  100000 kB\nMemAvailable:    1425000 kB\n")
    put("proc/loadavg", "0.42 0.30 0.20 1/180 1234\n")
    put("proc/uptime", "3725.31 12000.00\n")
    put("sys/class/thermal/thermal_zone0/temp", "51634\n")
    put("sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "1500000\n")
    put("sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "1800000\n")
    put("sys/devices/platform/soc/soc:firmware/get_throttled", "50005\n")
    if can:
        put("sys/class/net/can0/flags", flags + "\n")
        put("sys/class/net/can0/statistics/rx_packets", f"{rx}\n")


def sampler(tmp_path, clock, ip=IP_JSON):
    return cs.Sampler(root=tmp_path, disk=tmp_path, clock=clock, run=lambda cmd: ip)


def test_system_readings(tmp_path):
    fake_pi(tmp_path)
    clock = Clock()
    s = sampler(tmp_path, clock)
    s.sample()
    sysv = s.snapshot()["system"]
    assert sysv["cpu_temp_c"] == 51.6 and sysv["cpu_mhz"] == 1500 and sysv["cpu_max_mhz"] == 1800
    assert sysv["cpu_pct"] is None                       # needs two samples
    assert sysv["mem_pct"] == 25.0 and sysv["load_1m"] == 0.42 and sysv["uptime_s"] == 3725
    assert sysv["throttle"] == {"now": ["under-voltage", "throttled"],
                                "since_boot": ["under-voltage", "throttled"]}
    assert sysv["disk_free_gb"] is not None and 0 <= sysv["disk_pct"] <= 100

    fake_pi(tmp_path, cpu=(160, 50, 240, 1100, 50))      # +100 busy, +100 idle
    clock.t += 2
    s.sample()
    assert s.snapshot()["system"]["cpu_pct"] == 50.0


def test_obd_traffic_then_quiet(tmp_path):
    fake_pi(tmp_path, rx=1000)
    clock = Clock()
    s = sampler(tmp_path, clock)
    s.sample()
    assert s.obd["verdict"] == "checking" and s.obd["controller"] == "ERROR-ACTIVE"

    fake_pi(tmp_path, rx=1004)                           # 4 frames in 2 s: gateway keep-alive
    clock.t += 2
    s.sample()
    assert s.obd["verdict"] == "traffic" and s.obd["rx_per_s"] == 2.0 and s.obd["last_frame_s"] == 0

    for _ in range(3):                                   # counter stops: cable pulled
        clock.t += 2
        s.sample()
    assert s.obd["verdict"] == "quiet" and s.obd["rx_per_s"] == 0 and s.obd["last_frame_s"] == 6


def test_obd_never_seen_traffic_is_quiet(tmp_path):
    fake_pi(tmp_path, rx=0)
    clock = Clock()
    s = sampler(tmp_path, clock)
    s.sample()
    clock.t += 2
    s.sample()
    assert s.obd["verdict"] == "quiet" and s.obd["last_frame_s"] is None


def test_obd_problem_states(tmp_path):
    fake_pi(tmp_path, can=False)
    s = sampler(tmp_path, Clock())
    s.sample()
    assert s.obd["verdict"] == "no_interface" and s.obd["present"] is False

    fake_pi(tmp_path, flags="0x80")                      # interface exists but is down
    s = sampler(tmp_path, Clock())
    s.sample()
    assert s.obd["verdict"] == "down"

    fake_pi(tmp_path)
    s = sampler(tmp_path, Clock(), ip=IP_JSON.replace("ERROR-ACTIVE", "BUS-OFF"))
    s.sample()
    assert s.obd["verdict"] == "bus_off"


def test_missing_files_give_none_not_errors(tmp_path):
    s = sampler(tmp_path, Clock(), ip=None)              # empty root: not a Pi at all
    snap = s.snapshot()
    assert snap["system"]["cpu_temp_c"] is None and snap["system"]["throttle"] is None
    assert snap["obd"]["verdict"] == "no_interface"


def test_throttle_flags_and_parsers():
    assert cs.throttle_flags(0) == {"now": [], "since_boot": []}
    assert cs.throttle_flags(None) is None
    assert cs.can_link_info("garbage") == {}
    assert cs.cpu_times("") is None
    assert cs.meminfo(None) == (None, None)
