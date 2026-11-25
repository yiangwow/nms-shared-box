from box import Box, decide_hello, load, save


def rec(item_id, amount, max_amount=9999):
    return {"id": item_id, "amount": amount, "max": max_amount, "type": "Substance",
            "damage": 0.0, "auto": False, "installed": True}


def test_matches_empty_and_same():
    box = Box(slots={"0,0": rec("FUEL1", 10)})
    assert box.matches({})
    assert box.matches({"0,0": {"id": "FUEL1", "amount": 10}, "1,0": None})


def test_matches_rejects_different_amount_or_occupied():
    box = Box(slots={"0,0": rec("FUEL1", 10)})
    assert not box.matches({"0,0": {"id": "FUEL1", "amount": 9}})
    assert not box.matches({"0,0": None})
    assert not box.matches({"1,0": {"id": "FUEL1", "amount": 1}})


def test_replace_bumps_rev_and_seeds():
    box = Box()
    box.replace({"0,0": rec("FUEL1", 1)})
    assert box.rev == 1 and box.seeded and box.slots["0,0"]["amount"] == 1


def test_adjust_removes_and_clamps():
    box = Box(slots={"0,0": rec("FUEL1", 10), "1,0": rec("FUEL1", 5)})
    result = box.adjust({"FUEL1": -12}, {})
    total = sum(r["amount"] for r in box.slots.values() if r["id"] == "FUEL1")
    assert total == 3 and result == {"missing": {}, "overflow": {}}
    result = box.adjust({"FUEL1": -10}, {})
    assert box.slots == {} and result["missing"] == {"FUEL1": 7}


def test_adjust_adds_to_stack_then_free_slot():
    box = Box(slots={"0,0": rec("FUEL1", 9990)}, width=2, height=1)
    box.adjust({"FUEL1": 20}, {"FUEL1": rec("FUEL1", 1)})
    assert box.slots["0,0"]["amount"] == 9999
    assert box.slots["1,0"]["amount"] == 11 and box.slots["1,0"]["id"] == "FUEL1"


def test_adjust_overflow_when_full():
    box = Box(slots={"0,0": rec("LAND1", 1)}, width=1, height=1)
    result = box.adjust({"FUEL1": 5}, {"FUEL1": rec("FUEL1", 1)})
    assert result["overflow"] == {"FUEL1": 5}


def test_decide_hello():
    empty = Box()
    assert decide_hello(empty, None, {"0,0": rec("FUEL1", 1)}) == "seed"
    full = Box(slots={"0,0": rec("FUEL1", 1)}, seeded=True, rev=3)
    assert decide_hello(full, 2, {"5,5": rec("LAND1", 1)}) == "adopt"
    assert decide_hello(full, None, {}) == "adopt"
    assert decide_hello(full, None, {"0,0": rec("FUEL1", 1)}) == "adopt"
    assert decide_hello(full, None, {"0,0": rec("LAND1", 1)}) == "refuse"


def test_save_load_roundtrip(tmp_path):
    path = str(tmp_path / "data" / "box.json")
    box = Box(slots={"0,0": rec("FUEL1", 4)}, rev=7, seeded=True, width=10, height=6)
    save(box, path)
    again = load(path)
    assert again.slots == box.slots and again.rev == 7 and again.seeded and again.width == 10


def test_load_missing_file_is_empty(tmp_path):
    box = load(str(tmp_path / "nope.json"))
    assert box.slots == {} and box.rev == 0 and not box.seeded


def test_add_only_uses_valid_slots():
    box = Box(slots={}, valid_slots=["2,0", "0,1"])
    result = box.adjust({"GOLD": 20}, {"GOLD": rec("GOLD", 1, max_amount=5)})
    assert set(box.slots) == {"2,0", "0,1"} and result["overflow"] == {"GOLD": 10}


def test_save_load_keeps_valid_slots_and_recent_commits(tmp_path):
    path = str(tmp_path / "box.json")
    box = Box(slots={"0,0": rec("FUEL1", 1)}, rev=3, seeded=True, valid_slots=["0,0"])
    box.remember_commit("r1")
    save(box, path)
    back = load(path)
    assert back.valid_slots == ["0,0"] and back.recent_commits == ["r1"]


def test_remember_commit_keeps_the_last_few():
    box = Box()
    for i in range(40):
        box.remember_commit(str(i))
    assert box.recent_commits == [str(i) for i in range(8, 40)]
