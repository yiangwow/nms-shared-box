from sharedbox_lib.box_logic import LocalState, classify, deltas, expect_for, slot_key


def rec(item_id, amount):
    return {"id": item_id, "amount": amount, "max": 9999, "type": "Substance",
            "damage": 0.0, "auto": False, "installed": True}


def test_slot_key():
    assert slot_key(3, 1) == "3,1"


def test_expect_for_present_and_missing():
    slots = {"0,0": rec("FUEL1", 5)}
    assert expect_for(slots, ["0,0", "1,0"]) == {"0,0": {"id": "FUEL1", "amount": 5}, "1,0": None}


def test_deltas_consumed_and_added():
    before = {"0,0": rec("FUEL1", 10), "1,0": rec("LAND1", 4)}
    after = {"0,0": rec("FUEL1", 7), "1,0": rec("LAND1", 4), "2,0": rec("GOLD", 2)}
    d, items = deltas(before, after)
    assert d == {"FUEL1": -3, "GOLD": 2}
    assert items["GOLD"]["id"] == "GOLD" and "LAND1" not in items


def test_deltas_ignores_rearranging():
    before = {"0,0": rec("FUEL1", 10)}
    after = {"5,2": rec("FUEL1", 6), "6,2": rec("FUEL1", 4)}
    assert deltas(before, after) == ({}, {})


def test_classify():
    assert classify(12, 0, 12) == "take"
    assert classify(0, 12, 12) == "put"
    assert classify(12, 12, 12) == "move"
    assert classify(0, 1, 12) is None
    assert classify(None, 12, 12) == "put"


def test_local_state_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    state = LocalState(path)
    assert state.last_rev is None
    state.save(4)
    assert LocalState(path).last_rev == 4


def test_local_state_corrupt_or_empty_file_is_no_state(tmp_path):
    path = tmp_path / "state.json"
    for content in ("", "{not json", "[1, 2]", '{"last_rev": "7"}'):
        path.write_text(content)
        assert LocalState(str(path)).last_rev is None
    state = LocalState(str(path))
    state.save(9)
    assert LocalState(str(path)).last_rev == 9
    assert not (tmp_path / "state.json.tmp").exists()
