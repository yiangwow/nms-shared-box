"""Shared storage box for No Man's Sky, synced through a relay.
Design: ~/nms-shared-box/docs/superpowers/specs/2026-09-30-nms-shared-box-design.md"""
import ctypes
import logging
import os
import sys
import time
import tomllib
import traceback
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
# Helpers live two folders down so pyMHF doesn't try to load them as mods.
sys.path.insert(0, os.path.join(HERE, "_sharedbox"))

from pymhf import Mod, ModState  # noqa: E402
from pymhf.core._types import FUNCDEF  # noqa: E402
from pymhf.core.hooking import NOOP, hook_manager, manual_hook  # noqa: E402
from pymhf.gui.decorators import STRING  # noqa: E402

import nmspy.data.types as nms  # noqa: E402
from nmspy.common import gameData  # noqa: E402

from sharedbox_lib.box_logic import LocalState, classify, deltas, expect_for, slot_key  # noqa: E402
from sharedbox_lib.game_box import read_box, valid_keys, write_box  # noqa: E402
from sharedbox_lib.sync_client import SyncClient  # noqa: E402

logger = logging.getLogger("SharedBox")

# cGcPlayerState menu "move item" (was NMS.exe+0x580000 on nmspy 180132). Every menu move runs through it.
MOVE_PATTERN = "4C 89 4C 24 20 4C 89 44 24 18 48 89 54 24 10 55 56 41 55"
MOVE_HOOK_NAME = "SharedBox_MoveItem"
# Quick-transfer (the X key) is a separate function; found in NMS.exe build 25625620.
QUICK_PATTERN = "48 89 5C 24 18 55 57 41 57 48 81 EC 80 00 00 00"
QUICK_HOOK_NAME = "SharedBox_QuickTransfer"
# States seen in pymhf logs before/while a save loads. Any other state (APPVIEW, GALAXYMAP, ...) counts as in game.
OUT_OF_GAME_STATES = {"APPCORESERVICES", "APPGLOBALLOAD", "MODESELECTOR", "APPLOCALLOAD"}
FRAME_SECONDS = 0.1
WATCH_SECONDS = 0.25
RESEND_SECONDS = 5.0  # resend an unconfirmed commit this often (the relay ignores repeats)


def load_config():
    with open(os.path.join(HERE, "shared_box.toml"), "rb") as f:
        return tomllib.load(f)


@dataclass
class SharedBoxState(ModState):
    status: str = "starting"


class SharedBox(Mod):
    __author__ = "shared-box"
    __description__ = "Shared storage box synced through a relay"
    __version__ = "0.1.0"

    state = SharedBoxState()

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.box_index = int(self.cfg.get("box_inventory_index", 12))
        self.local_state = LocalState(os.path.join(HERE, "shared_box_state.json"))
        self.client = None
        self.baseline = None  # relay slots last written into (or confirmed in) the box
        self.hello_cache = ({}, None, 10, 6, None)
        self.in_game = False
        self.needs_resync = False
        self.disabled_reason = None
        self._ran = False
        self._result = 0
        self._originals = {}  # hook name -> the game's function, cached once the hook is verified
        self._epoch = 0  # client.connection_epoch we last synced at
        self.applied_rev = None  # relay revision the in-game box currently reflects
        self._next_frame = 0.0
        self._next_watch = 0.0
        self._loaded = False  # a save has been loaded at least once; the box is the save's, not the menu's
        # Our last approved move until the relay confirms it: {rid, base_rev, before, after, sent}.
        self._unacked = None
        self._ask_before = None  # the box as read just before the move being asked for
        # After we report a change ourselves, relay states older than this don't include it yet.
        self._min_rev = None

    @property
    @STRING("Status")
    def status(self):
        return self.state.status

    @status.setter
    def status(self, value):
        pass  # display only

    # --- helpers ---------------------------------------------------------------------------

    def _store(self):
        ps = gameData.player_state
        return ps.mInventories[self.box_index] if ps is not None else None

    @staticmethod
    def _find_hook(name):
        return next((h for h in hook_manager.hooks.values() if getattr(h, "_name", None) == name), None)

    def _ref_index(self, ref):
        if not ref:
            return None
        ptr = ctypes.c_uint64.from_address(ref).value
        if ptr:
            store = self._store()
            return self.box_index if store is not None and ptr == ctypes.addressof(store) else -1
        return ctypes.c_int32.from_address(ref + 8).value

    @staticmethod
    def _slot(p):
        if not p:
            return -1, -1
        return ctypes.c_int32.from_address(p).value, ctypes.c_int32.from_address(p + 4).value

    def _hello(self, store, local=None):
        return (read_box(store) if local is None else local, self.local_state.last_rev,
                store.miWidth, store.miHeight, valid_keys(store))

    def _expect_own_change(self):
        """We just sent an adjust: states the relay made before it can't be applied over the box."""
        cur = self.client.current_state()
        if cur is not None:
            self._raise_min_rev(cur[0] + 1)
        self.applied_rev = None
        self.needs_resync = True

    def _raise_min_rev(self, rev):
        self._min_rev = rev if self._min_rev is None else max(self._min_rev, rev)

    def _box_not_shared(self):
        """The relay refused this player (their box held items). Until they empty it, it is their own."""
        return not self.disabled_reason and self.client is not None and self.client.status.startswith("refused")

    # --- game state: loading a save means the relay's box wins, nothing is reported -------

    @nms.cTkFSMState.StateChange.after
    def on_state_change(self, this, lNewStateID, lpUserData, lbForceRestart):
        try:
            name = str(lNewStateID.contents)
            in_game = name not in OUT_OF_GAME_STATES  # APPVIEW, GALAXYMAP, anything unknown
            if in_game != self.in_game:
                logger.info(f"state {name}: {'in game' if in_game else 'out of game'}")
            self.in_game = in_game
            if not in_game:
                self.baseline = None
                self.applied_rev = None
                self.needs_resync = True
        except Exception:
            logger.error(f"state change handling failed:\n{traceback.format_exc()}")

    # The FSM state change fires BEFORE the save's inventories are loaded into memory, so it cannot
    # decide when to resync. This runs after the save is in memory; from here until the relay's box
    # is written, baseline is None and the watcher (which requires a baseline) reports nothing.
    @nms.cGcPlayerState.LoadFromData.after
    def on_save_loaded(self, this, lData, a3, lbNetworkClientLoad, lbIsLoadingForReset, a6):
        try:
            self.baseline = None
            self.applied_rev = None
            self.needs_resync = True
            self._loaded = True
            logger.info("save loaded; relay box will be applied")
        except Exception:
            logger.error(f"save load handling failed:\n{traceback.format_exc()}")

    # --- per-frame work on the game thread ------------------------------------------------

    @nms.cGcApplication.Update.after
    def on_frame(self, this):
        now = time.monotonic()
        if now < self._next_frame:
            return
        self._next_frame = now + FRAME_SECONDS
        try:
            self._tick(now)
        except Exception:
            logger.error(f"frame tick failed:\n{traceback.format_exc()}")

    def _tick(self, now):
        if self.disabled_reason:
            self.state.status = f"disabled: {self.disabled_reason}"
            return
        store = self._store() if self.in_game else None
        if store is None:
            return
        if self.client is None:
            if not self._loaded:
                # Before the first save load the box in memory is not this player's real box, so the
                # first-connect rules (seed / adopt / refuse) would judge the wrong contents.
                self.state.status = "waiting for the save to load"
                return
            for label, name in (("move", MOVE_HOOK_NAME), ("quick-transfer", QUICK_HOOK_NAME)):
                hook = self._find_hook(name)
                if hook is None or getattr(hook, "state", None) != "enabled":
                    self.disabled_reason = f"{label} hook not installed (game update?)"
                    logger.error(self.disabled_reason)
                    return
                self._originals[name] = hook.original
            self.hello_cache = self._hello(store)
            self.client = SyncClient(self.cfg["relay_url"], self.cfg["token"], self.cfg["player"],
                                     lambda: self.hello_cache)
            self.client.start()
        rtt = f" | last reply {self.client.last_rtt_ms:.0f} ms" if self.client.last_rtt_ms else ""
        self.state.status = f"{self.client.status}{rtt}"
        if not self.client.connected:
            # Keep the next hello current: a refused player who empties the box is accepted on retry.
            self.hello_cache = self._hello(store)

        # 0. Our last move: confirmed, reconnected (resend it), or rejected (record it as an adjust).
        self._check_unacked(now)
        if self.client.connection_epoch != self._epoch:
            self._on_reconnect(store, now)
        for rid in self.client.take_commit_rejected():
            self._on_commit_rejected(rid)

        # 1. Report changes made outside the menu (building/crafting from storage) before applying news.
        #    Throttled normally, but never skipped when a new state is about to overwrite the box.
        new = self.client.take_state()
        current = self.client.current_state()
        if new is None and current is not None and (
                self.needs_resync or (self.applied_rev is not None and current[0] > self.applied_rev)):
            new = current
        if self._unacked is not None:
            new = None  # hold relay news until the relay has confirmed or rejected our move
        if self.baseline is not None and (new is not None or now >= self._next_watch):
            self._next_watch = now + WATCH_SECONDS
            local = read_box(store)
            self.hello_cache = self._hello(store, local)
            if local != self.baseline and self.client.connected:
                changed, items = deltas(self.baseline, local)
                if changed:
                    logger.warning(f"box changed outside the menu, reporting {changed}")
                    self.client.adjust(changed, items)
                    self.baseline = local
                    self._expect_own_change()
                    new = None
                elif self._unacked is None:
                    # Same items, different slots: don't adopt a layout the relay doesn't have.
                    logger.info("box rearranged outside the menu; the relay's layout will be written back")
                    self.applied_rev = None
                    self.needs_resync = True

        # 2. Apply the relay's box: new states, or the latest one after a save load.
        if new is not None and self.applied_rev is not None and new[0] < self.applied_rev:
            new = None  # an old echo of a state we've already moved past
        if new is not None and self._min_rev is not None and new[0] < self._min_rev:
            new = None  # made before the relay saw our own report; a later state will include it
        if new is not None:
            rev, slots = new
            if read_box(store) != slots and not write_box(store, slots):
                self.disabled_reason = "relay box doesn't fit this box"
                logger.error(self.disabled_reason)
                return
            self.baseline = read_box(store)
            self.applied_rev = rev
            self._min_rev = None
            if self.needs_resync:
                logger.info(f"applied relay box, rev {rev}")
            self.needs_resync = False
            self.local_state.save(rev)

    def _check_unacked(self, now):
        u = self._unacked
        committed = self.client.take_committed()
        if u is None:
            return
        cur = self.client.current_state()
        if u["rid"] in committed or (cur is not None and cur[0] >= u["base_rev"] + 1 and cur[1] == u["after"]):
            self._unacked = None
            # The relay applied our commit at base_rev + 1, so no older state holds this move.
            self._raise_min_rev(u["base_rev"] + 1)
            return
        if (self.client.connected and self.client.connection_epoch == self._epoch
                and now - u["sent"] >= RESEND_SECONDS):
            logger.warning(f"no confirmation for commit {u['rid']}, resending")
            self.client.commit(u["rid"], u["after"])
            u["sent"] = now

    def _on_reconnect(self, store, now):
        """A new connection. The relay's box will be written over ours, so first make sure it knows
        everything that happened here: the move whose commit may have been lost, then any change the
        watcher would have seen while we were offline."""
        logger.info("reconnected; relay box will be applied")
        self._epoch = self.client.connection_epoch
        self._min_rev = None
        if self._unacked is not None:
            logger.warning(f"resending commit {self._unacked['rid']} after reconnecting")
            self.client.commit(self._unacked["rid"], self._unacked["after"])
            self._unacked["sent"] = now
        self.applied_rev = None
        self.needs_resync = True
        if self.baseline is not None:
            local = read_box(store)
            changed, items = deltas(self.baseline, local)
            if changed:
                logger.warning(f"box changed while offline, reporting {changed}")
                self.client.adjust(changed, items)
                self._expect_own_change()
            # Everything up to `local` is now reported, so the watcher keeps counting from here.
            self.baseline = local

    def _on_commit_rejected(self, rid):
        """The relay's hold expired before our commit arrived, so it never recorded the move. The move
        did happen in game, so record it as an adjust instead of undoing it."""
        u = self._unacked
        if u is not None and u["rid"] == rid:
            self._unacked = None
            changed, items = deltas(u["before"], u["after"])
            if changed:
                logger.warning(f"relay rejected commit {rid}; reporting the move as {changed}")
                self.client.adjust(changed, items)
                self._expect_own_change()
                return
        logger.warning("relay rejected a commit; the relay's box will be written back")
        self.applied_rev = None
        self.needs_resync = True

    # --- the hooks: ask the relay before any move that touches the box ---------------------

    def _run_hook(self, name, handler, args):
        """Run `handler(original, *args)` and always return an int. An exception escaping a detour would
        make pyMHF drop it while the NOOP flag stays set, and every such move would then return nothing.
        The game's function is never run twice."""
        self._ran = False
        self._result = 0
        original = self._originals.get(name)
        try:
            if original is None:
                original = self._originals[name] = self._find_hook(name).original
            return int(handler(original, *args))
        except Exception:
            logger.error(f"{name} failed:\n{traceback.format_exc()}")
        if self._ran:  # the game's move already happened
            return self._result
        if original is None:
            return 0
        try:
            return int(original(*args))
        except Exception:
            logger.error(f"the game's function failed:\n{traceback.format_exc()}")
            return 0

    def _run_original(self, original, *args):
        result = original(*args)
        self._ran = True
        self._result = int(result or 0)
        return result

    def _refuse_reason(self):
        """Why a move touching the box must be refused right now, or None."""
        if self.disabled_reason:
            return f"box locked (disabled: {self.disabled_reason})"
        if not self.in_game:
            return "box locked (not in game)"
        if self.client is None or not self.client.connected:
            return f"box locked (reconnecting: {self.state.status})"
        if self.baseline is None:
            return "box locked (no baseline yet, waiting for the relay's box)"
        if self.client.commit_rejected_pending:
            return "commit rejected, resyncing"
        if self._unacked is not None:
            return "box updating (last move not confirmed yet)"
        if self.client.connection_epoch != self._epoch:
            return "box updating (reconnected, resync pending)"
        pending = self.client.current_state()
        if self.applied_rev is None:
            return "box updating (no applied revision yet)"
        if pending is not None and pending[0] > self.applied_rev:
            return f"box updating (relay update pending, rev {pending[0]} > {self.applied_rev})"
        return None

    def _ask(self, kind, keys, store):
        """Ask the relay to approve a move. Returns the request id, or None if refused."""
        reason = self._refuse_reason()
        if reason:
            logger.info(f"{reason}; {kind} refused")
            return None
        local = read_box(store)
        outcome, rid = self.client.request(expect_for(local, keys), kind, self.applied_rev)
        if outcome != "ok":
            if outcome == "timeout":
                self.client.commit(rid, local)  # nothing moved; release the relay's hold now
            if outcome == "deny" and self.client.last_deny_reason == "stale":
                self.needs_resync = True  # our box is behind the relay's; reload it
                outcome = "stale"
            logger.info(f"{kind} refused ({outcome})")
            return None
        self._ask_before = local
        return rid

    def _finish(self, rid, store):
        after = read_box(store)
        self._unacked = {"rid": rid, "base_rev": self.applied_rev, "before": self._ask_before,
                         "after": after, "sent": time.monotonic()}
        self.client.commit(rid, after)
        self.baseline = after
        # An approved request holds the relay's lock, so after our commit it is exactly at base_rev + 1
        # with exactly our box. Bump now so the next move isn't refused while the broadcast is in flight.
        if self.applied_rev is not None:
            self.applied_rev += 1

    @NOOP
    @manual_hook(
        MOVE_HOOK_NAME,
        pattern=MOVE_PATTERN,
        func_def=FUNCDEF(
            restype=ctypes.c_int32,
            argtypes=[
                ctypes.c_ulonglong,  # rcx: cGcPlayerState*
                ctypes.c_ulonglong,  # rdx: source inventory ref*
                ctypes.c_ulonglong,  # r8: destination inventory ref*
                ctypes.c_ulonglong,  # r9: source slot*
                ctypes.c_ulonglong,  # stack5: destination slot*
                ctypes.c_int32,      # stack6: amount
            ],
        ),
        detour_time="before",
    )
    def move_item(self, this, src, dst, src_slot, dst_slot, amount):
        return self._run_hook(MOVE_HOOK_NAME, self._move, (this, src, dst, src_slot, dst_slot, amount))

    def _move(self, original, this, src, dst, src_slot, dst_slot, amount):
        args = (this, src, dst, src_slot, dst_slot, amount)
        kind = classify(self._ref_index(src), self._ref_index(dst), self.box_index)
        if kind is None or (kind == "take" and self._box_not_shared()):
            return self._run_original(original, *args)
        keys = []
        if kind in ("take", "move"):
            keys.append(slot_key(*self._slot(src_slot)))
        if kind in ("put", "move"):
            keys.append(slot_key(*self._slot(dst_slot)))
        store = self._store()
        rid = self._ask(kind, keys, store)
        if rid is None:
            return 0
        result = self._run_original(original, *args)
        self._finish(rid, store)
        logger.info(f"{kind} x{result} done in {self.client.last_rtt_ms:.0f} ms")
        return result

    @NOOP
    @manual_hook(
        QUICK_HOOK_NAME,
        pattern=QUICK_PATTERN,
        func_def=FUNCDEF(
            restype=ctypes.c_int32,
            argtypes=[
                ctypes.c_ulonglong,  # rcx: source cGcInventoryStore* (direct pointer)
                ctypes.c_ulonglong,  # rdx: destination inventory ref*
                ctypes.c_ulonglong,  # r8: source slot*
                ctypes.c_ulonglong,  # r9: destination slot* ((-1,-1) = game picks)
                ctypes.c_ulonglong,  # stack5: cGcInventoryElement* being moved
                ctypes.c_int32,      # stack6: amount
            ],
        ),
        detour_time="before",
    )
    def quick_transfer(self, src_store, dst_ref, src_slot, dst_slot, element, amount):
        return self._run_hook(QUICK_HOOK_NAME, self._quick,
                              (src_store, dst_ref, src_slot, dst_slot, element, amount))

    def _quick(self, original, src_store, dst_ref, src_slot, dst_slot, element, amount):
        args = (src_store, dst_ref, src_slot, dst_slot, element, amount)
        store = self._store()
        src_box = store is not None and src_store == ctypes.addressof(store)
        dst_box = self._ref_index(dst_ref) == self.box_index
        if not src_box and not dst_box:
            return self._run_original(original, *args)
        kind = "move" if src_box and dst_box else "take" if src_box else "put"
        if kind == "take" and self._box_not_shared():
            return self._run_original(original, *args)
        keys = []
        if src_box:
            keys.append(slot_key(*self._slot(src_slot)))
        if dst_box and self._slot(dst_slot) != (-1, -1):
            keys.append(slot_key(*self._slot(dst_slot)))
        rid = self._ask(kind, keys, store)
        if rid is None:
            return 0
        result = self._run_original(original, *args)
        self._finish(rid, store)
        logger.info(f"quick {kind} x{result} done in {self.client.last_rtt_ms:.0f} ms")
        return result
