"""Pure helpers for the shared box mod. No game or network access, so they're testable outside the game."""
import json
import os


def slot_key(x, y):
    return f"{x},{y}"


def expect_for(slots, keys):
    """What the relay should see in these slots for our request to be valid (None = empty)."""
    return {k: ({"id": slots[k]["id"], "amount": slots[k]["amount"]} if k in slots else None) for k in keys}


def _totals(slots):
    out = {}
    for value in slots.values():
        out[value["id"]] = out.get(value["id"], 0) + value["amount"]
    return out


def deltas(before, after):
    """Per-item change in totals between two box snapshots, plus a record for each changed item."""
    a, b = _totals(before), _totals(after)
    changed = {i: b.get(i, 0) - a.get(i, 0) for i in set(a) | set(b)}
    changed = {i: n for i, n in changed.items() if n}
    items = {}
    for value in list(after.values()) + list(before.values()):
        if value["id"] in changed and value["id"] not in items:
            items[value["id"]] = dict(value)
    return changed, items


def classify(src_index, dst_index, box_index):
    from_box, to_box = src_index == box_index, dst_index == box_index
    if from_box and to_box:
        return "move"
    if from_box:
        return "take"
    if to_box:
        return "put"
    return None


class LocalState:
    """Remembers the last relay revision this PC synced, so a returning player adopts the relay's box.
    A missing, empty or corrupt file counts as never synced."""

    def __init__(self, path):
        self.path = path
        self.last_rev = None
        try:
            with open(path) as f:
                rev = json.load(f).get("last_rev")
            if isinstance(rev, int) and not isinstance(rev, bool):
                self.last_rev = rev
        except Exception:
            pass

    def save(self, rev):
        if rev == self.last_rev:
            return
        self.last_rev = rev
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_rev": rev}, f)
        os.replace(tmp, self.path)
