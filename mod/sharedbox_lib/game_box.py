"""Read and write the shared box in game memory. Game thread only; needs nmspy (runs inside NMS)."""
import ctypes
import logging

import nmspy.data.basic_types as basics
import nmspy.data.enums as enums
import nmspy.data.exported_types as nmse
from nmspy.data.types import engine_modules
from pymhf.core.memutils import get_addressof

from sharedbox_lib.box_logic import slot_key

logger = logging.getLogger("SharedBox")


def record(el):
    return {"id": str(el.Id), "amount": int(el.Amount), "max": int(el.MaxAmount), "type": el.Type.name,
            "damage": float(el.DamageFactor), "auto": bool(el.AddedAutomatically),
            "installed": bool(el.FullyInstalled)}


def read_box(store):
    return {slot_key(el.Index.X, el.Index.Y): record(el) for el in store.mStore}


def valid_keys(store):
    """The slots this box actually has (mxValidSlots), so the relay only puts new stacks there."""
    return [slot_key(x, y) for y in range(store.miHeight) for x in range(store.miWidth)
            if store.mxValidSlots[y][x]]


def _element(key, rec):
    x, y = (int(v) for v in key.split(","))
    return nmse.cGcInventoryElement(
        Id=basics.TkID0x10(rec["id"].encode()),
        Index=nmse.cGcInventoryIndex(x, y),
        Amount=rec["amount"],
        DamageFactor=rec["damage"],
        MaxAmount=rec["max"],
        Type=enums.cGcInventoryType[rec["type"]],
        AddedAutomatically=rec["auto"],
        FullyInstalled=rec["installed"],
    )


def ensure_capacity(vec, needed, floor=0):
    """Grow an nmspy tk_vector to hold `needed` elements. nmspy's expand() grows by 1.5x the current
    length, which is 0 for an empty list, so we allocate ourselves with the same game allocator.
    `floor` is the minimum new length (a full box's worth), so this runs at most once per box."""
    if vec.allocated_size >= needed:
        return True
    size = vec._template_type_size
    new_len = max(needed, floor, 8)
    mm = engine_modules.gMemoryManager
    new_addr = mm.Malloc(new_len * size, 0, 0, 0, 16, -1)
    if not new_addr:
        logger.error("could not allocate memory to grow the box")
        return False
    logger.info("grew the box: allocated %d -> %d elements", vec.allocated_size, new_len)
    old_addr = get_addressof(vec._ptr) if vec.allocated_size else 0
    if vec.vector_size and old_addr:
        ctypes.memmove(int(new_addr), old_addr, size * vec.vector_size)
    # The old buffer is deliberately leaked: it was allocated by the game, and nmspy's
    # cTkMemoryManager.Free docstring (nmspy/data/types.py) says freeing game-allocated memory
    # will almost certainly crash the game.
    vec._ptr = ctypes.cast(int(new_addr), ctypes.POINTER(vec._template_type))
    vec.allocated_size = new_len
    return True


def validate_target(store, target):
    """Return None if every relay slot fits this box, else a description of the first bad one."""
    for key, rec in target.items():
        try:
            x, y = (int(v) for v in key.split(","))
        except ValueError:
            return f"bad slot key {key!r}"
        if not (0 <= x < store.miWidth and 0 <= y < store.miHeight) or not store.mxValidSlots[y][x]:
            return f"slot {key} does not exist in this box"
        if rec.get("type") not in enums.cGcInventoryType.__members__:
            return f"slot {key} has unknown type {rec.get('type')!r}"
        amount, mx = rec.get("amount"), rec.get("max")
        if isinstance(amount, bool) or isinstance(mx, bool) or not isinstance(amount, int) \
                or not isinstance(mx, int) or not 0 < amount <= mx:
            return f"slot {key} has bad amount/max {amount!r}/{mx!r}"
        if not isinstance(rec.get("id"), str):
            return f"slot {key} has bad id"
    return None


def write_box(store, target):
    """Make the in-game box match `target` exactly. Returns False (writing nothing) if it doesn't fit."""
    problem = validate_target(store, target)
    if problem:
        logger.error(f"relay box doesn't fit this box: {problem}")
        return False
    vec = store.mStore
    current = {slot_key(vec[i].Index.X, vec[i].Index.Y): i for i in range(len(vec))}
    new_keys = [k for k in target if k not in current]
    if new_keys and not ensure_capacity(vec, len(vec) + len(new_keys), store.miWidth * store.miHeight):
        return False
    remove = []
    for key, i in current.items():
        want = target.get(key)
        if want is None:
            remove.append(i)
            continue
        el = vec[i]
        if str(el.Id) == want["id"]:
            el.Amount = want["amount"]
            el.MaxAmount = want["max"]
        else:
            new = _element(key, want)
            ctypes.memmove(ctypes.addressof(el), ctypes.addressof(new), ctypes.sizeof(new))
    for i in sorted(remove, reverse=True):
        del vec[i]
    for key in new_keys:
        if not vec.append(_element(key, target[key])):
            return False
    return True
