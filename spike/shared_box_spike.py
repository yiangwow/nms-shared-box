# THROWAWAY SPIKE for the shared-box idea. Answers one question:
# can we write items into a storage container at runtime, and do they survive save + reload?
# Deliberately cautious: never builds an item from scratch, never grows the item vector.

import ctypes
import json
import os
import time
from dataclasses import dataclass
import functools
import traceback
from logging import getLogger

from pymhf import Mod, ModState
from pymhf.gui.decorators import BOOLEAN, INTEGER, gui_button

import nmspy.data.exported_types as nmse
import nmspy.data.types as nms
from pymhf.core.memutils import get_addressof
from pymhf.core.hooking import NOOP, get_caller, hook_manager, manual_hook
from pymhf.core._types import FUNCDEF
from pymhf.core.memutils import map_struct
from nmspy.common import gameData

logger = getLogger("SharedBoxSpike")

def _logged(func):
    # The GUI swallows callback exceptions silently; surface them in the pymhf log.
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger.info(f"Button pressed: {func.__name__}")
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.error(f"{func.__name__} failed:\n{traceback.format_exc()}")

    return wrapper


BOX_INDEX = 12  # Container 0

DUMP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spike_dump.txt")


@dataclass
class SpikeState(ModState):
    inventory_index: int = 0
    block_box: bool = False


def _describe(i, inv):
    items = []
    for item in inv.mStore:
        items.append(
            {
                "id": str(item.Id),
                "x": item.Index.X,
                "y": item.Index.Y,
                "amount": item.Amount,
                "max": item.MaxAmount,
                "type": getattr(item.Type, "name", repr(item.Type)),
            }
        )
    return {
        "index": i,
        "name": str(inv.mInventoryName),
        "w": inv.miWidth,
        "h": inv.miHeight,
        "capacity": inv.miCapacity,
        "vector": f"{inv.mStore.vector_size}/{inv.mStore.allocated_size}",
        "items": items,
    }


class SharedBoxSpike(Mod):
    __author__ = "yiangwow"
    __description__ = "Throwaway test: write items into a storage container"

    state = SpikeState()

    @property
    @INTEGER("Inventory index")
    def inventory_index(self):
        return self.state.inventory_index

    @inventory_index.setter
    def inventory_index(self, value):
        self.state.inventory_index = value

    @property
    @BOOLEAN("Block taking from Container 0")
    def block_box(self):
        return self.state.block_box

    @block_box.setter
    def block_box(self, value):
        self.state.block_box = value

    def _inv(self):
        ps = gameData.player_state
        if ps is None:
            logger.error("Player state not loaded yet. Load your save first.")
            return None
        return ps.mInventories[self.state.inventory_index]

    @gui_button("1. Dump all inventories to file")
    @_logged
    def dump(self):
        ps = gameData.player_state
        if ps is None:
            logger.error("Player state not loaded yet. Load your save first.")
            return
        out = [_describe(i, inv) for i, inv in enumerate(ps.mInventories)]
        for i, inv in enumerate(ps.mShipInventoriesCargo):
            d = _describe(i, inv)
            d["index"] = f"ship{i}_cargo"
            out.append(d)
        logger.info(f"Primary ship slot: {ps.miPrimaryShip}")
        with open(DUMP_PATH, "a") as f:
            f.write(f"===== {time.strftime('%H:%M:%S')}\n")
            f.write(json.dumps(out, indent=1) + "\n")
        for d in out:
            if d["items"]:
                logger.info(f"[{d['index']}] {d['name']!r} {d['w']}x{d['h']} items={len(d['items'])}")
        logger.info(f"Wrote dump to {DUMP_PATH}")

    @gui_button("2. Add 10 to first stack in selected inventory")
    @_logged
    def bump(self):
        if (inv := self._inv()) is None:
            return
        if len(inv.mStore) == 0:
            logger.error("Selected inventory is empty.")
            return
        item = inv.mStore[0]
        if item.Amount + 10 > item.MaxAmount:
            logger.error(f"{item.Id} would exceed max {item.MaxAmount}; not touching it.")
            return
        item.Amount += 10
        logger.info(f"{item.Id} at ({item.Index.X},{item.Index.Y}) is now {item.Amount}")

    @gui_button("3. Copy first stack into next empty slot")
    @_logged
    def copy_stack(self):
        if (inv := self._inv()) is None:
            return
        store = inv.mStore
        if len(store) == 0:
            logger.error("Selected inventory is empty. Put one stack in it first.")
            return
        if store.vector_size >= store.allocated_size:
            logger.error(f"No spare room in item list ({store.vector_size}/{store.allocated_size}); skipping.")
            return
        used = {(it.Index.X, it.Index.Y) for it in store}
        target = None
        for y in range(inv.miHeight):
            for x in range(inv.miWidth):
                if (x, y) not in used and inv.mxValidSlots[y][x]:
                    target = (x, y)
                    break
            if target:
                break
        if target is None:
            logger.error("No empty valid slot.")
            return
        new = nmse.cGcInventoryElement.from_buffer_copy(store[0])
        new.Index.X, new.Index.Y = target
        ok = store.append(new)
        logger.info(f"Copied {new.Id} x{new.Amount} into slot {target}: {'ok' if ok else 'FAILED'}")


    # --- Spike 2: watch-only hooks. Do menu moves go through cGcInventoryStore::Add / Remove? ---

    def _which_addr(self, addr):
        ps = gameData.player_state
        if ps is None:
            return None
        for label, arr in (("inv", ps.mInventories), ("shipcargo", ps.mShipInventoriesCargo)):
            for i, inv in enumerate(arr):
                if ctypes.addressof(inv) == addr:
                    return f"{label}[{i}]"
        return f"other@0x{addr:X}"

    def _which(self, this):
        """Map a cGcInventoryStore pointer to a player inventory label, or None if it isn't one."""
        ps = gameData.player_state
        if ps is None:
            return None
        addr = get_addressof(this)
        for label, arr in (("inv", ps.mInventories), ("shipcargo", ps.mShipInventoriesCargo)):
            for i, inv in enumerate(arr):
                if ctypes.addressof(inv) == addr:
                    return f"{label}[{i}]"
        return f"other@0x{addr:X}"

    @get_caller
    @nms.cGcInventoryStore.Add.after
    def after_add(self, this, result, lItem):
        try:
            which = self._which(this)
            if which is None or which.startswith("other"):
                return  # skip load-time noise; only player inventories matter here
            item = lItem.contents
            caller = self.after_add.caller_address()
            logger.info(f"HOOK Add    -> {which} {item.Id} x{item.Amount} caller=NMS.exe+0x{caller:X}")
        except Exception:
            logger.error(f"HOOK Add log failed:\n{traceback.format_exc()}")

    @nms.cGcInventoryStore.Remove.after
    def after_remove(self, this, lIndex, liAmountOverride):
        try:
            logger.info(f"HOOK Remove -> {self._which(this)} slot={lIndex} amount_override={liAmountOverride}")
        except Exception:
            logger.error(f"HOOK Remove log failed:\n{traceback.format_exc()}")

    # Spike 3: NMS.exe+0x582750 is a cGcPlayerState method that stacks an item into an inventory
    # (found by walking up from the Add caller). Args read from its prologue + call site at 0x6F2B51.
    @get_caller
    @manual_hook(
        "PlayerState_AddToInventory",
        offset=0x582750,
        func_def=FUNCDEF(
            restype=ctypes.c_int32,
            argtypes=[
                ctypes.c_ulonglong,  # rcx: cGcPlayerState*
                ctypes.c_ulonglong,  # rdx: destination cGcInventoryStore*
                ctypes.c_ulonglong,  # r8: cGcInventoryElement*
                ctypes.c_ulonglong,  # r9: unknown pointer
                ctypes.c_int32,      # stack+0x20
                ctypes.c_bool,       # stack+0x28
            ],
        ),
        detour_time="after",
    )
    def after_ps_add(self, this, inv, item, a4, a5, a6):
        try:
            caller = self.after_ps_add.caller_address()
            el = map_struct(item, nmse.cGcInventoryElement) if item else None
            desc = f"{el.Id} x{el.Amount}" if el is not None else "no item"
            logger.info(
                f"HOOK PS_Add -> {self._which_addr(inv) if inv else 'null'} {desc} "
                f"a4=0x{a4:X} a5={a5} a6={a6} caller=NMS.exe+0x{caller:X}"
            )
        except Exception:
            logger.error(f"HOOK PS_Add log failed:\n{traceback.format_exc()}")

    # Spike 4: NMS.exe+0x580000 = the menu "move item" function (every menu move reaches PS_Add from here).
    # Args read from its prologue/body: rdx/r8 = src/dst inventory refs {u64 inv*, i32, i32 id},
    # r9 = src slot*, stack5 = dst slot*, stack6 = amount. Runs BEFORE anything changes.
    def _ref(self, ref):
        if not ref:
            return "null"
        inv_ptr, a, b = ctypes.c_uint64.from_address(ref).value, ctypes.c_int32.from_address(ref + 8).value, ctypes.c_int32.from_address(ref + 12).value
        return f"{self._which_addr(inv_ptr) if inv_ptr else 'byid'}(ptr=0x{inv_ptr:X},{a},{b})"

    def _slot(self, p):
        if not p:
            return "null"
        return f"({ctypes.c_int32.from_address(p).value},{ctypes.c_int32.from_address(p + 4).value})"

    # Spike 5: same hook, now able to cancel. NOOP means pyMHF never runs the original itself;
    # our return value becomes the result, so we call the original ourselves when a move is allowed.
    @NOOP
    @manual_hook(
        "PlayerState_MoveItem",
        offset=0x580000,
        func_def=FUNCDEF(
            restype=ctypes.c_int32,
            argtypes=[
                ctypes.c_ulonglong,  # rcx: cGcPlayerState*
                ctypes.c_ulonglong,  # rdx: source inventory ref* {u64 inv*, i32 inventory index, i32}
                ctypes.c_ulonglong,  # r8: destination inventory ref*
                ctypes.c_ulonglong,  # r9: source slot*
                ctypes.c_ulonglong,  # stack5: destination slot*
                ctypes.c_int32,      # stack6: amount
            ],
        ),
        detour_time="before",
    )
    def move_item(self, this, src, dst, src_slot, dst_slot, amount):
        original = next(h for h in hook_manager.hooks.values() if getattr(h, "_name", None) == "PlayerState_MoveItem").original
        try:
            return self._move_item(original, this, src, dst, src_slot, dst_slot, amount)
        except Exception:
            # Never leave the game without its move: on any bug in our logic, run the original.
            logger.error(f"HOOK Move failed, running original:\n{traceback.format_exc()}")
            return original(this, src, dst, src_slot, dst_slot, amount)

    def _move_item(self, original, this, src, dst, src_slot, dst_slot, amount):
        src_idx = ctypes.c_int32.from_address(src + 8).value if src else None
        dst_idx = ctypes.c_int32.from_address(dst + 8).value if dst else None
        desc = (f"src={self._ref(src)} slot={self._slot(src_slot)} "
                f"dst={self._ref(dst)} slot={self._slot(dst_slot)} amount={amount}")
        if self.state.block_box and src_idx == BOX_INDEX and dst_idx != BOX_INDEX:
            logger.info(f"HOOK Move BLOCKED -> {desc}")
            return 0
        result = original(this, src, dst, src_slot, dst_slot, amount)
        logger.info(f"HOOK Move allowed -> {desc} result={result}")
        return result
