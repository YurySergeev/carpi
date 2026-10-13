import json
from collections import Counter

import pytest

import carpi_presets as cp


def _rates(weights, rows=1200):
    s = cp.Schedule(weights)
    counts = Counter(p for _ in range(rows) for p in s.next_row())
    return {p: counts[p] / rows for p in weights}


def test_default_reproduces_old_fast_slow_polling():
    old_fast = ["0C", "0B", "70", "04", "0E", "06", "44", "34", "23", "49", "11", "0D"]
    old_slow = ["05", "0F", "07", "42", "33", "46", "2F", "3C", "15", "56", "43", "1F"]
    s = cp.Schedule(cp.DEFAULT["weights"])
    for i in range(36):
        assert s.next_row() == old_fast + [old_slow[i % 12]]


def test_rates_follow_weights():
    rates = _rates({"0C": 20, "06": 10, "07": 5, "05": 1})
    assert rates == pytest.approx({"0C": 1.0, "06": 0.5, "07": 0.25, "05": 0.05})


def test_equal_weights_spread_across_rows():
    s = cp.Schedule({"0C": 4, "05": 1, "0F": 1, "07": 1, "42": 1})
    rows = [s.next_row() for _ in range(8)]
    assert all(len(r) == 2 for r in rows)           # rpm + exactly one slow value each row


def test_top_weight_value_is_on_every_row():
    s = cp.Schedule({"05": 3, "0C": 7, "07": 2})
    assert all("0C" in s.next_row() for _ in range(100))


def test_builtins_are_valid_and_in_catalog_order():
    order = list(cp.CATALOG)
    for preset in cp.BUILTIN.values():
        assert cp.validate(preset) == preset
        keys = list(preset["weights"])
        assert keys == sorted(keys, key=order.index)


@pytest.mark.parametrize("bad, msg", [
    ({"name": "", "weights": {"0C": 1}}, "name"),
    ({"name": "x/y", "weights": {"0C": 1}}, "name"),
    ({"name": "ok", "weights": {"FF": 1}}, "unknown"),
    ({"name": "ok", "weights": {"0C": 21}}, "weight"),
    ({"name": "ok", "weights": {"0C": 1.5}}, "weight"),
    ({"name": "ok", "weights": {"0C": True}}, "weight"),
    ({"name": "ok", "weights": {"0C": 0}}, "at least one"),
    ({"name": "ok", "weights": {"0C": 1}, "id": "../etc"}, "id"),
    ("nope", "object"),
])
def test_validate_rejects(bad, msg):
    with pytest.raises(cp.PresetError, match=msg):
        cp.validate(bad)


def test_validate_drops_zeros_and_sorts():
    p = cp.validate({"name": " Idle study ", "weights": {"07": 2, "0C": 5, "05": 0}})
    assert p == {"id": "idle-study", "name": "Idle study", "weights": {"0C": 5, "07": 2}}


def test_store_round_trip(tmp_path):
    store = cp.Store(tmp_path)
    assert store.active_id() == "default"
    assert [p["id"] for p in store.presets()] == ["default", "fuel-trim", "pulls"]

    saved = store.save({"name": "Idle study", "weights": {"0C": 5, "06": 5}})
    assert (tmp_path / "presets" / "idle-study.json").exists()
    assert store.presets()[-1] == dict(saved, builtin=False)

    store.activate("idle-study")
    assert json.loads((tmp_path / "active_preset.json").read_text())["weights"] == {"0C": 5, "06": 5}
    with pytest.raises(cp.PresetError, match="active"):
        store.delete("idle-study")

    store.save({"name": "Idle study", "weights": {"0C": 5, "06": 5, "07": 1}})   # edit active
    assert store.active()["weights"] == {"0C": 5, "06": 5, "07": 1}

    store.activate("default")
    store.delete("idle-study")
    assert [p["id"] for p in store.presets()] == ["default", "fuel-trim", "pulls"]


def test_store_protects_builtins(tmp_path):
    store = cp.Store(tmp_path)
    with pytest.raises(cp.PresetError, match="built in"):
        store.save({"name": "Default", "weights": {"0C": 1}})
    with pytest.raises(cp.PresetError, match="built-in"):
        store.delete("pulls")
    with pytest.raises(cp.PresetError):
        store.delete("../tag")


def test_tag(tmp_path):
    store = cp.Store(tmp_path)
    assert store.tag() == ""
    store.set_tag("post-pcv")
    assert (tmp_path / "tag.txt").read_text() == "post-pcv\n"
    assert store.tag() == "post-pcv"
    with pytest.raises(cp.PresetError):
        store.set_tag("two words")
    store.set_tag("")
    assert store.tag() == ""


def test_read_active(tmp_path):
    path = tmp_path / "active_preset.json"
    assert cp.read_active(path) is cp.DEFAULT                    # no file
    path.write_text("{ half a fi")
    assert cp.read_active(path) is None                          # bad file: caller keeps current
    path.write_text(json.dumps({"id": "x", "name": "X", "weights": {"0C": 1}}))
    assert cp.read_active(path)["id"] == "x"


def test_supported(tmp_path):
    store = cp.Store(tmp_path)
    assert store.supported() is None
    (tmp_path / "supported.json").write_text(json.dumps({"pids": ["01", "0C", "20", "70"]}))
    assert store.supported() == ["0C", "70"]
