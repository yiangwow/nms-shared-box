"""The true shared box: state and rules. The server wraps this; nothing here does network I/O."""
import json
import os
import tempfile
from dataclasses import dataclass, field


def _same(a, b):
    """Two slot contents match if both are empty, or both hold the same item id and amount."""
    if a is None or b is None:
        return a is None and b is None
    return a["id"] == b["id"] and a["amount"] == b["amount"]


@dataclass
class Box:
    slots: dict = field(default_factory=dict)  # "x,y" -> item record
    rev: int = 0
    seeded: bool = False
    width: int = 10
    height: int = 6

    def matches(self, expect):
        """True if every slot in `expect` looks the same here as on the requesting player's screen."""
        return all(_same(self.slots.get(key), want) for key, want in expect.items())

    def replace(self, slots):
        self.slots = {key: dict(value) for key, value in slots.items()}
        self.seeded = True
        self.rev += 1

    def adjust(self, deltas, items):
        """Apply changes seen outside the menu: {item_id: +n or -n}. Removals clamp at zero."""
        missing, overflow = {}, {}
        for item_id, delta in deltas.items():
            if delta < 0:
                left = self._remove(item_id, -delta)
                if left:
                    missing[item_id] = left
            elif delta > 0:
                left = self._add(item_id, delta, items.get(item_id))
                if left:
                    overflow[item_id] = left
        self.seeded = True
        self.rev += 1
        return {"missing": missing, "overflow": overflow}

    def _remove(self, item_id, amount):
        for key in sorted((k for k, v in self.slots.items() if v["id"] == item_id), reverse=True):
            take = min(amount, self.slots[key]["amount"])
            self.slots[key]["amount"] -= take
            amount -= take
            if self.slots[key]["amount"] == 0:
                del self.slots[key]
            if amount == 0:
                break
        return amount

    def _add(self, item_id, amount, template):
        for value in self.slots.values():
            if value["id"] == item_id and value["amount"] < value["max"]:
                put = min(amount, value["max"] - value["amount"])
                value["amount"] += put
                amount -= put
                if amount == 0:
                    return 0
        if template is None:
            return amount
        for y in range(self.height):
            for x in range(self.width):
                key = f"{x},{y}"
                if key in self.slots:
                    continue
                put = min(amount, template["max"])
                self.slots[key] = dict(template, amount=put)
                amount -= put
                if amount == 0:
                    return 0
        return amount


def decide_hello(box, last_rev, local_slots):
    """What to do when a player connects.

    seed:   nobody has shared a box yet, so this player's box becomes the shared one.
    adopt:  this player takes the relay's box (they synced before, or their box is empty or identical).
    refuse: a new player whose box already holds different items; syncing would overwrite them.
    """
    if not box.seeded:
        return "seed"
    if last_rev is not None or not local_slots:
        return "adopt"
    same = set(local_slots) == set(box.slots) and all(_same(local_slots[k], box.slots[k]) for k in local_slots)
    return "adopt" if same else "refuse"


def save(box, path):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"rev": box.rev, "seeded": box.seeded, "width": box.width,
                   "height": box.height, "slots": box.slots}, f)
    os.replace(tmp, path)


def load(path):
    if not os.path.exists(path):
        return Box()
    with open(path) as f:
        data = json.load(f)
    return Box(slots=data["slots"], rev=data["rev"], seeded=data["seeded"],
               width=data.get("width", 10), height=data.get("height", 6))
