# NMS Shared Box Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One No Man's Sky storage container shared live between two PC players, where every menu move touching it is approved by a relay first, so items can't be duplicated.

**Architecture:** A small asyncio WebSocket relay (`relay/`) owns the true box and approves requests one at a time. An NMS.py mod (`mod/`) hooks the game's menu "move item" function (`NMS.exe`, found by byte pattern), asks the relay before any move that touches the box, writes relay updates into game memory on the game thread, and reports changes made outside the menu.

**Tech Stack:** Python 3.12 (relay, Docker) / Windows Python 3.11 (mod, inside the game via pyMHF 0.2.4 + nmspy 180132), `websockets==13.1` (asyncio server + sync client), pytest 8.

**Spec:** `docs/superpowers/specs/2026-09-30-nms-shared-box-design.md`

## Global Constraints

- Box = `player_state.mInventories[12]` (Container 0) by default; configurable as `box_inventory_index`.
- Move hook pattern: `4C 89 4C 24 20 4C 89 44 24 18 48 89 54 24 10 55 56 41 55` (unique in the current NMS.exe; it was at RVA `0x580000`).
- Move hook signature: `(u64 player_state, u64 src_ref, u64 dst_ref, u64 src_slot, u64 dst_slot, i32 amount) -> i32 amount_moved`. Ref layout: `{u64 inv_ptr, i32 inventory_index, i32 -1}`; menus pass `inv_ptr = 0`.
- Hook waits at most **1.5 s** for the relay; no reply = refused.
- Relay holds an approved request open for at most **10 s** (`PENDING_SECONDS`) waiting for its commit.
- Watcher interval **250 ms**; frame work throttled to every **100 ms**.
- Relay: container `nms-relay`, port 8080, **no published port**, public only via the Cloudflare tunnel at `nms.xyyoutube.com`; env `NMS_RELAY_TOKEN`, `NMS_RELAY_DATA=/data/box.json`.
- Git: author the owner's git identity, **no Co-Authored-By / Claude attribution**, never push to `main` directly, stage files by name (never `git add -A`). Tasks 1–3 on branch `feat/shared-box` (PR 1, relay); Tasks 4–8 on branch `feat/shared-box-mod` (PR 2, mod).
- Never append to an nmspy `tk_vector` without spare capacity except through `ensure_capacity` (nmspy's `expand()` is broken for this).
- The mod must never block a move it can't classify: moves not touching the box always run the original.

## Review Focus

1. **Relay link drops between "ok" and "commit"**: other players must get "busy" until the hold expires. A late commit is rejected and nothing is duplicated. (Task 2: `test_pending_expires_and_late_commit_rejected`)
2. **Slow link, the hook times out**: the move is refused and the hold is released at once by an unchanged commit, not 10 s later. (Task 2: `test_commit_with_unchanged_slots_releases_hold`; Task 6 hook code)
3. **Both players drop different items into the same empty slot at once**: exactly one succeeds. (Task 2: `test_same_empty_slot_race`)
4. **Loading a save (or an older save) while connected**: the box shows the relay's contents, and nothing gets reported as a change, so reloads can't duplicate. (Task 6: FSM state-change hook; Task 7 checklist G)
5. **Taking into a full exosuit**: the game moves fewer items than asked. The relay must record what actually moved. (Task 2: `test_commit_records_actual_snapshot`; Task 7 checklist H)

---

## File Structure

```
relay/box.py            true box: rules, adjust, hello decision, save/load (pure)
relay/server.py         asyncio WebSocket relay around Box
relay/requirements.txt  websockets pin
relay/Dockerfile        container image
relay/tests/conftest.py
relay/tests/test_box.py
relay/tests/test_server.py
mod/sharedbox_lib/__init__.py
mod/sharedbox_lib/box_logic.py    pure helpers used by the mod (slot keys, expectations, deltas, classify, LocalState)
mod/sharedbox_lib/sync_client.py  background relay client (sync websockets)
mod/sharedbox_lib/game_box.py     read/write the box in game memory (needs nmspy; game only)
mod/shared_box.py                 the pyMHF Mod: hooks, frame loop, status
mod/shared_box.toml.example
mod/start-nms-shared.bat
mod/tests/conftest.py
mod/tests/test_box_logic.py
mod/tests/test_sync_client.py
tools/install_mod.sh   copy the mod into NMSpyMods on this PC
tools/relay_ping.py    probe a relay without touching the box
tools/fake_player.py   act as a second player from the command line
docs/IN_GAME_CHECKLIST.md
docs/FRIEND_SETUP.md
```

Installed layout on each PC (pyMHF loads every `.py` in the mods folder **and one level below**, so helpers sit two levels down):
```
NMSpyMods/shared_box.py
NMSpyMods/shared_box.toml
NMSpyMods/start-nms-shared.bat
NMSpyMods/_sharedbox/sharedbox_lib/*.py
```

---

### Task 1: Relay box logic

**Files:**
- Create: `relay/box.py`, `relay/tests/conftest.py`, `relay/tests/test_box.py`, `.gitignore`

**Interfaces:**
- Produces: `Box(slots: dict, rev: int, seeded: bool, width: int, height: int)` with `matches(expect: dict) -> bool`, `replace(slots: dict) -> None`, `adjust(deltas: dict[str,int], items: dict[str,dict]) -> {"missing": dict, "overflow": dict}`; `decide_hello(box, last_rev, local_slots) -> "seed"|"adopt"|"refuse"`; `save(box, path)`, `load(path) -> Box`.
- Slot key: `"x,y"`. Item record: `{"id": str, "amount": int, "max": int, "type": str, "damage": float, "auto": bool, "installed": bool}`.

- [ ] **Step 1: Branch, venv, gitignore**

```bash
cd ~/nms-shared-box && git checkout -b feat/shared-box
python3 -m venv .venv && .venv/bin/pip install --quiet websockets==13.1 pytest==8.3.3
printf '.venv/\n__pycache__/\n*.pyc\n.pytest_cache/\nmod/shared_box.toml\n' > .gitignore
```

- [ ] **Step 2: Write the failing tests**

`relay/tests/conftest.py`:
```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

`relay/tests/test_box.py`:
```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/nms-shared-box && .venv/bin/pytest relay/tests/test_box.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'box'`

- [ ] **Step 4: Implement `relay/box.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest relay/tests/test_box.py -q`
Expected: `9 passed`

- [ ] **Step 6: Commit**

```bash
git add .gitignore relay/box.py relay/tests/conftest.py relay/tests/test_box.py
git commit -m "feat(relay): box rules, adjust, hello decision, persistence"
```

---

### Task 2: Relay server

**Files:**
- Create: `relay/server.py`, `relay/requirements.txt`, `relay/tests/test_server.py`

**Interfaces:**
- Consumes: Task 1 `Box`, `decide_hello`, `load`, `save`.
- Produces: `Relay(token: str, path: str)` with `.handler(conn)`, `.box`, `.pending`; module constant `PENDING_SECONDS`; `async start(relay, host, port) -> websockets Server`.
- Wire protocol (JSON text frames):
  - C→S `{"t":"hello","token","player","last_rev":int|null,"slots":{},"width":int,"height":int}`, optional `"probe": true`
  - S→C `{"t":"state","rev","slots"}` | `{"t":"refused","reason":"box_not_empty"}` | `{"t":"error","reason":"bad_token"}` | probe: `{"t":"pong","rev","seeded","stacks"}`
  - C→S `{"t":"request","id","kind","expect":{"x,y": {"id","amount"}|null}}` → S→C `{"t":"ok","id"}` | `{"t":"deny","id","reason":"busy"|"stale"}`
  - C→S `{"t":"commit","id","slots"}` → broadcast `state`, or S→C `{"t":"commit_rejected","id"}`
  - C→S `{"t":"adjust","deltas":{id:int},"items":{id:record}}` → broadcast `state`

- [ ] **Step 1: Write the failing tests**

`relay/requirements.txt`:
```
websockets==13.1
```

`relay/tests/test_server.py`:
```python
import asyncio
import json

from websockets.asyncio.client import connect

import server as relay_server


def rec(item_id, amount):
    return {"id": item_id, "amount": amount, "max": 9999, "type": "Substance",
            "damage": 0.0, "auto": False, "installed": True}


async def boot(tmp_path, token="t"):
    relay = relay_server.Relay(token, str(tmp_path / "box.json"))
    srv = await relay_server.start(relay, "127.0.0.1", 0)
    port = list(srv.sockets)[0].getsockname()[1]
    return relay, srv, f"ws://127.0.0.1:{port}"


async def shutdown(srv):
    srv.close()
    await srv.wait_closed()


async def hello(url, slots=None, last_rev=None, token="t", player="p", probe=False):
    ws = await connect(url)
    msg = {"t": "hello", "token": token, "player": player, "last_rev": last_rev,
           "slots": slots or {}, "width": 10, "height": 6}
    if probe:
        msg["probe"] = True
    await ws.send(json.dumps(msg))
    return ws, json.loads(await ws.recv())


async def recv_type(ws, kind):
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 2))
        if msg["t"] == kind:
            return msg


def test_bad_token_rejected(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        _, reply = await hello(url, token="wrong")
        assert reply == {"t": "error", "reason": "bad_token"}
        await shutdown(srv)
    asyncio.run(run())


def test_probe_does_not_seed(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        _, reply = await hello(url, probe=True)
        assert reply["t"] == "pong" and reply["seeded"] is False
        assert relay.box.seeded is False
        await shutdown(srv)
    asyncio.run(run())


def test_first_player_seeds_then_new_player_with_items_refused(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        _, state = await hello(url, {"0,0": rec("FUEL1", 5)})
        assert state["t"] == "state" and state["slots"]["0,0"]["amount"] == 5
        _, reply = await hello(url, {"3,3": rec("LAND1", 1)}, player="friend")
        assert reply == {"t": "refused", "reason": "box_not_empty"}
        _, reply = await hello(url, {}, player="friend")
        assert reply["t"] == "state" and "0,0" in reply["slots"]
        await shutdown(srv)
    asyncio.run(run())


def test_request_ok_commit_broadcasts(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take",
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        assert (await recv_type(a, "ok"))["id"] == "r1"
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {"0,0": rec("FUEL1", 2)}}))
        state = await recv_type(b, "state")
        assert state["slots"]["0,0"]["amount"] == 2 and state["rev"] == 2
        await shutdown(srv)
    asyncio.run(run())


def test_second_request_while_pending_is_busy(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        expect = {"0,0": {"id": "FUEL1", "amount": 5}}
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "expect": expect}))
        await recv_type(a, "ok")
        await b.send(json.dumps({"t": "request", "id": "r2", "kind": "take", "expect": expect}))
        assert (await recv_type(b, "deny"))["reason"] == "busy"
        await shutdown(srv)
    asyncio.run(run())


def test_stale_expect_denied_after_commit(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        expect = {"0,0": {"id": "FUEL1", "amount": 5}}
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "expect": expect}))
        await recv_type(a, "ok")
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {}}))
        await recv_type(b, "state")
        await b.send(json.dumps({"t": "request", "id": "r2", "kind": "take", "expect": expect}))
        assert (await recv_type(b, "deny"))["reason"] == "stale"
        await shutdown(srv)
    asyncio.run(run())


def test_same_empty_slot_race(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        empty = {"4,4": None}
        await a.send(json.dumps({"t": "request", "id": "ra", "kind": "put", "expect": empty}))
        assert (await recv_type(a, "ok"))["id"] == "ra"
        await b.send(json.dumps({"t": "request", "id": "rb", "kind": "put", "expect": empty}))
        assert (await recv_type(b, "deny"))["reason"] == "busy"
        await a.send(json.dumps({"t": "commit", "id": "ra",
                                 "slots": {"0,0": rec("FUEL1", 5), "4,4": rec("LAND1", 3)}}))
        await recv_type(b, "state")
        await b.send(json.dumps({"t": "request", "id": "rb2", "kind": "put", "expect": empty}))
        assert (await recv_type(b, "deny"))["reason"] == "stale"
        await shutdown(srv)
    asyncio.run(run())


def test_commit_records_actual_snapshot(tmp_path):
    # Taking into a full exosuit moves fewer than asked; the commit snapshot is what counts.
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 50)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take",
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 50}}}))
        await recv_type(a, "ok")
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {"0,0": rec("FUEL1", 38)}}))
        await recv_type(a, "state")
        assert relay.box.slots["0,0"]["amount"] == 38
        await shutdown(srv)
    asyncio.run(run())


def test_commit_with_unchanged_slots_releases_hold(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, state = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take",
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": state["slots"]}))
        await recv_type(a, "state")
        assert relay.pending is None
        await shutdown(srv)
    asyncio.run(run())


def test_pending_expires_and_late_commit_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(relay_server, "PENDING_SECONDS", 0.1)

    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        expect = {"0,0": {"id": "FUEL1", "amount": 5}}
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "expect": expect}))
        await recv_type(a, "ok")
        await asyncio.sleep(0.2)
        await b.send(json.dumps({"t": "request", "id": "r2", "kind": "take", "expect": expect}))
        assert (await recv_type(b, "ok"))["id"] == "r2"
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {}}))
        assert (await recv_type(a, "commit_rejected"))["id"] == "r1"
        assert relay.box.slots["0,0"]["amount"] == 5
        await shutdown(srv)
    asyncio.run(run())


def test_adjust_broadcasts(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        await a.send(json.dumps({"t": "adjust", "deltas": {"FUEL1": -2}, "items": {}}))
        state = await recv_type(b, "state")
        assert state["slots"]["0,0"]["amount"] == 3
        await shutdown(srv)
    asyncio.run(run())


def test_state_survives_restart(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        await hello(url, {"0,0": rec("FUEL1", 5)})
        await shutdown(srv)
        relay2, srv2, url2 = await boot(tmp_path)
        _, reply = await hello(url2, {}, player="friend")
        assert reply["slots"]["0,0"]["amount"] == 5
        await shutdown(srv2)
    asyncio.run(run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest relay/tests/test_server.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'server'`

- [ ] **Step 3: Implement `relay/server.py`**

```python
"""WebSocket relay that owns the shared box and handles one request at a time.

Protocol: see docs/superpowers/specs/2026-09-30-nms-shared-box-design.md §4 and the plan's Task 2.
"""
import asyncio
import json
import logging
import os
import time

from websockets.asyncio.server import serve

from box import decide_hello, load, save

logger = logging.getLogger("nms-relay")

# How long an approved request stays open waiting for its commit. While it is open every other
# request is refused as "busy", so a player who disconnects mid-move cannot cause a duplicate.
PENDING_SECONDS = 10.0
HELLO_SECONDS = 10.0


class Relay:
    def __init__(self, token, path):
        self.token = token
        self.path = path
        self.box = load(path)
        self.clients = set()
        self.pending = None  # (connection, request id, deadline)
        self.lock = asyncio.Lock()

    def _state_msg(self):
        return json.dumps({"t": "state", "rev": self.box.rev, "slots": self.box.slots})

    async def _broadcast(self):
        msg = self._state_msg()
        for conn in list(self.clients):
            try:
                await conn.send(msg)
            except Exception:
                self.clients.discard(conn)

    async def handler(self, conn):
        try:
            hello = json.loads(await asyncio.wait_for(conn.recv(), HELLO_SECONDS))
        except Exception:
            return
        if hello.get("t") != "hello" or hello.get("token") != self.token:
            await conn.send(json.dumps({"t": "error", "reason": "bad_token"}))
            return
        player = str(hello.get("player", "?"))
        if hello.get("probe"):
            await conn.send(json.dumps({"t": "pong", "rev": self.box.rev, "seeded": self.box.seeded,
                                        "stacks": len(self.box.slots)}))
            return
        async with self.lock:
            local = hello.get("slots") or {}
            decision = decide_hello(self.box, hello.get("last_rev"), local)
            if decision == "refuse":
                await conn.send(json.dumps({"t": "refused", "reason": "box_not_empty"}))
                logger.info("%s refused: their box already holds other items", player)
                return
            if decision == "seed":
                self.box.width = int(hello.get("width", self.box.width))
                self.box.height = int(hello.get("height", self.box.height))
                self.box.replace(local)
                save(self.box, self.path)
                logger.info("%s seeded the box with %d stacks", player, len(self.box.slots))
            self.clients.add(conn)
            await conn.send(self._state_msg())
        logger.info("%s connected (%s)", player, decision)
        try:
            async for raw in conn:
                await self._on_message(conn, player, json.loads(raw))
        except Exception as e:
            logger.info("%s connection ended: %s", player, e)
        finally:
            self.clients.discard(conn)
            logger.info("%s disconnected", player)

    async def _on_message(self, conn, player, msg):
        changed = False
        async with self.lock:
            if self.pending and self.pending[2] < time.monotonic():
                logger.warning("request %s expired without a commit", self.pending[1])
                self.pending = None
            kind = msg.get("t")
            if kind == "request":
                rid = msg.get("id")
                if self.pending:
                    await conn.send(json.dumps({"t": "deny", "id": rid, "reason": "busy"}))
                elif not self.box.matches(msg.get("expect") or {}):
                    await conn.send(json.dumps({"t": "deny", "id": rid, "reason": "stale"}))
                else:
                    self.pending = (conn, rid, time.monotonic() + PENDING_SECONDS)
                    await conn.send(json.dumps({"t": "ok", "id": rid}))
                    logger.info("%s %s approved (%s)", player, msg.get("kind"), rid)
            elif kind == "commit":
                if self.pending and self.pending[0] is conn and self.pending[1] == msg.get("id"):
                    self.pending = None
                    self.box.replace(msg.get("slots") or {})
                    save(self.box, self.path)
                    changed = True
                else:
                    await conn.send(json.dumps({"t": "commit_rejected", "id": msg.get("id")}))
                    logger.warning("%s sent a commit with no open request: %s", player, msg.get("id"))
            elif kind == "adjust":
                result = self.box.adjust(msg.get("deltas") or {}, msg.get("items") or {})
                save(self.box, self.path)
                changed = True
                logger.info("%s adjust %s -> %s", player, msg.get("deltas"), result)
            if changed:
                await self._broadcast()


async def start(relay, host, port):
    return await serve(relay.handler, host, port)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    relay = Relay(os.environ["NMS_RELAY_TOKEN"], os.environ.get("NMS_RELAY_DATA", "/data/box.json"))
    await start(relay, "0.0.0.0", int(os.environ.get("NMS_RELAY_PORT", "8080")))
    logger.info("nms-relay listening, box rev %d, %d stacks", relay.box.rev, len(relay.box.slots))
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest relay/tests -q`
Expected: `21 passed` (9 box + 12 server)

- [ ] **Step 5: Commit**

```bash
git add relay/server.py relay/requirements.txt relay/tests/test_server.py
git commit -m "feat(relay): websocket relay with ordered approvals, commits and adjust"
```

---

### Task 3: Deploy the relay

**Files:**
- Create: `relay/Dockerfile`, `tools/relay_ping.py`
- Modify: `CLAUDE.md` (deploy section)
- Modify (other repo, branch `feat/nms-relay`): `~/is-infra/projects/self-hosted/docker-compose.yml`, `~/is-infra/projects/self-hosted/.env.example`

**Interfaces:**
- Consumes: Task 2 `server.py` (entrypoint, env vars), the probe hello.
- Produces: the live relay at `wss://nms.xyyoutube.com`, and the token (kept only in the server `.env` and each player's `shared_box.toml`).

- [ ] **Step 1: Dockerfile and ping tool**

`relay/Dockerfile`:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY box.py server.py ./
RUN mkdir /data && chown 1000:1000 /data
USER 1000
ENV NMS_RELAY_DATA=/data/box.json
CMD ["python", "-u", "server.py"]
```

`tools/relay_ping.py`:
```python
"""Check a relay is reachable and the token works, without touching the box.
Usage: python tools/relay_ping.py wss://nms.xyyoutube.com TOKEN"""
import json
import sys

from websockets.sync.client import connect

url, token = sys.argv[1], sys.argv[2]
with connect(url, open_timeout=10) as ws:
    ws.send(json.dumps({"t": "hello", "token": token, "player": "ping", "probe": True}))
    print(ws.recv(timeout=10))
```

- [ ] **Step 2: Build and probe locally**

```bash
cd ~/nms-shared-box/relay && docker build -q -t nms-relay:test . \
  && docker run -d --rm --name nms-relay-test -e NMS_RELAY_TOKEN=local -p 18080:8080 nms-relay:test \
  && sleep 2 && ../.venv/bin/python ../tools/relay_ping.py ws://127.0.0.1:18080 local; docker stop nms-relay-test
```
Expected: `{"t": "pong", "rev": 0, "seeded": false, "stacks": 0}`. If Docker isn't available in WSL, skip this step and rely on Step 7.

- [ ] **Step 3: Commit, create the GitHub repo, open the PR**

Update the `## Deploy` section of `CLAUDE.md` to:
```markdown
## Deploy
- Relay code: this repo, cloned on tt-server at `/home/tt/nms-shared-box` (pull manually: `ssh tt@192.168.1.9 'cd ~/nms-shared-box && git pull'`).
- Compose service `nms-relay` lives in `~/is-infra`; `git push && deploy` rebuilds it.
- Token: `NMS_RELAY_TOKEN` in `projects/self-hosted/.env` on the server; same value in each player's `shared_box.toml`.
- Public hostname `nms.xyyoutube.com → http://nms-relay:8080` is set in the Cloudflare dashboard (token-managed tunnel).
- Probe: `.venv/bin/python tools/relay_ping.py wss://nms.xyyoutube.com <token>`
```
```bash
cd ~/nms-shared-box
git add relay/Dockerfile tools/relay_ping.py CLAUDE.md
git commit -m "feat(relay): container image and probe tool"
gh repo create yiangwow/nms-shared-box --private --source . --remote origin
git push -u origin main && git push -u origin feat/shared-box
gh pr create --base main --head feat/shared-box --title "Shared box: relay" --body "Relay for the NMS shared box. Spec: docs/superpowers/specs/2026-09-30-nms-shared-box-design.md"
```
(`main` only holds the reviewed spec commit; all code goes through the PR.) **Ask the user to merge the PR**, or get their go-ahead to merge it. Then `gh pr view --json state` must show `MERGED`.

- [ ] **Step 4: is-infra service (branch + PR)**

```bash
cd ~/is-infra && git checkout main && git pull && git checkout -b feat/nms-relay
```
In `projects/self-hosted/docker-compose.yml`, add after the `cloudflared` service (same indentation as the other services):
```yaml
  # ============================================
  # NMS shared box relay (tunnel only, no published port)
  # ============================================
  nms-relay:
    build:
      context: /home/tt/nms-shared-box/relay
    container_name: nms-relay
    restart: unless-stopped
    environment:
      - NMS_RELAY_TOKEN=${NMS_RELAY_TOKEN}
      - NMS_RELAY_DATA=/data/box.json
    volumes:
      - nms-relay-data:/data
    networks:
      - self-hosted
```
Under the top-level `volumes:` key add `  nms-relay-data:`. In `.env.example` add `NMS_RELAY_TOKEN=` with a comment line `# NMS shared box relay token (shared with both players' mods)`.
```bash
docker compose -f projects/self-hosted/docker-compose.yml config -q 2>&1 | grep -v "variable is not set" || true
git add projects/self-hosted/docker-compose.yml projects/self-hosted/.env.example
git commit -m "feat: nms-relay service for the NMS shared box"
git push -u origin feat/nms-relay
gh pr create --base main --title "nms-relay service" --body "Adds the NMS shared box relay. Tunnel only, no published port."
```
Expected: `config -q` prints nothing besides unset-variable warnings.

- [ ] **Step 5: Server preparation (before the is-infra PR merges)**

```bash
ssh tt@192.168.1.9 'test -d ~/nms-shared-box || git clone git@github.com:yiangwow/nms-shared-box.git ~/nms-shared-box; cd ~/nms-shared-box && git pull && git log --oneline -1'
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
ssh tt@192.168.1.9 "grep -q '^NMS_RELAY_TOKEN=' ~/is-infra/projects/self-hosted/.env || echo 'NMS_RELAY_TOKEN=$TOKEN' >> ~/is-infra/projects/self-hosted/.env; grep -c '^NMS_RELAY_TOKEN=' ~/is-infra/projects/self-hosted/.env"
```
Expected: the latest relay commit hash, then `1`. If `~/is-infra/projects/self-hosted/.env` is not the live `.env` path, find it with `ssh tt@192.168.1.9 'ls ~/is-infra/projects/self-hosted/.env'` first and stop if it's missing. Read the token back with `ssh tt@192.168.1.9 "grep NMS_RELAY_TOKEN ~/is-infra/projects/self-hosted/.env"` when filling in `shared_box.toml`; never commit it.

- [ ] **Step 6: Merge is-infra PR and deploy** (ask the user to merge, or get their go-ahead)

```bash
cd ~/is-infra && git checkout main && git pull && deploy
sleep 60 && ssh tt@192.168.1.9 'docker ps --filter name=nms-relay --format "{{.Status}}"; docker logs --tail 5 nms-relay'
```
Expected: `Up ...` and a log line `nms-relay listening, box rev 0, 0 stacks`.

- [ ] **Step 7: Cloudflare hostname (user, manual) and public probe**

Ask the user to add it in the Cloudflare dashboard: Zero Trust → Networks → Tunnels → (the tunnel) → Public Hostname → Add. Subdomain `nms`, domain `xyyoutube.com`, type `HTTP`, URL `nms-relay:8080`.
```bash
cd ~/nms-shared-box && .venv/bin/python tools/relay_ping.py wss://nms.xyyoutube.com "$(ssh tt@192.168.1.9 "grep '^NMS_RELAY_TOKEN=' ~/is-infra/projects/self-hosted/.env | cut -d= -f2-")"
```
Expected: `{"t": "pong", "rev": 0, "seeded": false, "stacks": 0}`

---

### Task 4: Mod pure logic

**Files:**
- Create: `mod/sharedbox_lib/__init__.py` (empty), `mod/sharedbox_lib/box_logic.py`, `mod/tests/conftest.py`, `mod/tests/test_box_logic.py`

**Interfaces:**
- Produces: `slot_key(x:int, y:int) -> str`; `expect_for(slots: dict, keys: list[str]) -> dict`; `deltas(before: dict, after: dict) -> (dict[str,int], dict[str,dict])`; `classify(src_index, dst_index, box_index) -> "take"|"put"|"move"|None`; `LocalState(path)` with `.last_rev: int|None`, `.save(rev: int)`.

- [ ] **Step 1: New branch from the merged relay, then write the failing tests**

```bash
cd ~/nms-shared-box && git checkout main && git pull && git checkout -b feat/shared-box-mod
```

`mod/tests/conftest.py`:
```python
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "mod"))
sys.path.insert(0, os.path.join(ROOT, "relay"))
```

`mod/tests/test_box_logic.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest mod/tests/test_box_logic.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sharedbox_lib'`

- [ ] **Step 3: Implement `mod/sharedbox_lib/box_logic.py`**

```python
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
    """Remembers the last relay revision this PC synced, so a returning player adopts the relay's box."""

    def __init__(self, path):
        self.path = path
        self.last_rev = None
        if os.path.exists(path):
            with open(path) as f:
                self.last_rev = json.load(f).get("last_rev")

    def save(self, rev):
        if rev == self.last_rev:
            return
        self.last_rev = rev
        with open(self.path, "w") as f:
            json.dump({"last_rev": rev}, f)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest mod/tests/test_box_logic.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add mod/sharedbox_lib/__init__.py mod/sharedbox_lib/box_logic.py mod/tests/conftest.py mod/tests/test_box_logic.py
git commit -m "feat(mod): pure box helpers"
```

---

### Task 5: Mod sync client

**Files:**
- Create: `mod/sharedbox_lib/sync_client.py`, `mod/tests/test_sync_client.py`
- Modify: `mod/tests/conftest.py` (add relay fixture)

**Interfaces:**
- Consumes: Task 2 protocol and `server.Relay`/`server.start` (tests only).
- Produces: `SyncClient(url, token, player, hello_source)` where `hello_source() -> (slots: dict, last_rev: int|None, width: int, height: int)` is called on each (re)connect; methods `start()`, `stop()`, `request(expect: dict, kind: str, timeout: float = 1.5) -> (outcome: "ok"|"deny"|"timeout"|"offline", rid: str|None)`, `commit(rid: str, slots: dict)`, `adjust(deltas: dict, items: dict)`, `take_state() -> (rev, slots)|None` (new states only), `current_state() -> (rev, slots)|None` (latest, always); attributes `status: str`, `connected: bool`, `last_rtt_ms: float|None`.

- [ ] **Step 1: Write the failing tests**

Append to `mod/tests/conftest.py`:
```python
import asyncio
import threading

import pytest


class RelayThread:
    """Runs a real relay on a background event loop for client tests."""

    def __init__(self, path, token="t", port=0):
        import server as relay_server

        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)

            async def boot():
                self.relay = relay_server.Relay(token, path)
                self.srv = await relay_server.start(self.relay, "127.0.0.1", port)
                self.url = f"ws://127.0.0.1:{list(self.srv.sockets)[0].getsockname()[1]}"
                ready.set()

            self.loop.run_until_complete(boot())
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        assert ready.wait(5)

    def stop(self):
        async def down():
            self.srv.close()
            await self.srv.wait_closed()

        asyncio.run_coroutine_threadsafe(down(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def relay(tmp_path):
    r = RelayThread(str(tmp_path / "box.json"))
    yield r
    r.stop()
```

`mod/tests/test_sync_client.py`:
```python
import time

from sharedbox_lib.sync_client import SyncClient


def rec(item_id, amount):
    return {"id": item_id, "amount": amount, "max": 9999, "type": "Substance",
            "damage": 0.0, "auto": False, "installed": True}


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def client(url, slots, last_rev=None, player="p", token="t"):
    c = SyncClient(url, token, player, lambda: (slots, last_rev, 10, 6))
    c.start()
    return c


def test_connect_seeds_and_delivers_state(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)})
    try:
        assert wait_for(lambda: a.connected)
        rev, slots = a.take_state()
        assert rev == 1 and slots["0,0"]["amount"] == 5
        assert a.take_state() is None
        assert a.current_state()[0] == 1
    finally:
        a.stop()


def test_request_commit_reaches_other_client(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)}, player="a")
    assert wait_for(lambda: a.connected)
    b = client(relay.url, {}, player="b")
    try:
        assert wait_for(lambda: b.connected)
        b.take_state()
        outcome, rid = a.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take")
        assert outcome == "ok" and a.last_rtt_ms is not None
        a.commit(rid, {"0,0": rec("FUEL1", 1)})
        assert wait_for(lambda: (b.current_state() or (0, {}))[0] == 2)
        assert b.current_state()[1]["0,0"]["amount"] == 1
    finally:
        a.stop()
        b.stop()


def test_stale_request_denied(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)})
    try:
        assert wait_for(lambda: a.connected)
        outcome, _ = a.request({"0,0": {"id": "FUEL1", "amount": 4}}, "take")
        assert outcome == "deny"
    finally:
        a.stop()


def test_refused_new_player_with_items(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)}, player="a")
    assert wait_for(lambda: a.connected)
    b = client(relay.url, {"1,1": rec("LAND1", 2)}, player="b")
    try:
        assert wait_for(lambda: b.status.startswith("refused"))
        assert b.request({}, "take")[0] == "offline"
    finally:
        a.stop()
        b.stop()


def test_offline_when_no_relay():
    c = client("ws://127.0.0.1:9", {})
    try:
        time.sleep(0.3)
        assert not c.connected
        assert c.request({}, "take")[0] == "offline"
    finally:
        c.stop()


def test_reconnects_after_relay_restart(tmp_path):
    from conftest import RelayThread

    path = str(tmp_path / "box.json")
    r1 = RelayThread(path)
    port = int(r1.url.rsplit(":", 1)[1])
    a = SyncClient(r1.url, "t", "a", lambda: ({"0,0": rec("FUEL1", 5)}, None, 10, 6))
    a.RECONNECT_SECONDS = 0.1
    a.start()
    try:
        assert wait_for(lambda: a.connected)
        r1.stop()
        assert wait_for(lambda: not a.connected)
        r2 = RelayThread(path, port=port)
        try:
            assert wait_for(lambda: a.connected, timeout=10)
        finally:
            r2.stop()
    finally:
        a.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest mod/tests/test_sync_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sharedbox_lib.sync_client'`

- [ ] **Step 3: Implement `mod/sharedbox_lib/sync_client.py`**

```python
"""Background client for the shared box relay.

The game thread calls request/commit/adjust and polls take_state; a network thread owns the
connection, reconnects on its own, and hands replies back through events.
"""
import json
import logging
import threading
import time
import uuid

from websockets.sync.client import connect

logger = logging.getLogger("SharedBox")


class SyncClient:
    RECONNECT_SECONDS = 2.0

    def __init__(self, url, token, player, hello_source):
        self.url = url
        self.token = token
        self.player = player
        self.hello_source = hello_source
        self.status = "starting"
        self.last_rtt_ms = None
        self._ws = None
        self._lock = threading.Lock()
        self._new_state = None
        self._current = None
        self._waiters = {}  # request id -> [Event, reply]
        self._stop = threading.Event()
        self._thread = None

    @property
    def connected(self):
        return self._ws is not None and self.status == "connected"

    def start(self):
        self._thread = threading.Thread(target=self._run, name="SharedBoxSync", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self):
        while not self._stop.is_set():
            self.status = "connecting"
            try:
                with connect(self.url, open_timeout=5, close_timeout=1) as ws:
                    slots, last_rev, width, height = self.hello_source()
                    ws.send(json.dumps({"t": "hello", "token": self.token, "player": self.player,
                                        "last_rev": last_rev, "slots": slots,
                                        "width": width, "height": height}))
                    first = json.loads(ws.recv(timeout=10))
                    if first["t"] != "state":
                        self.status = f"{first['t']}: {first.get('reason', '?')}"
                        logger.error(f"relay said {first}")
                        self._stop.wait(30)
                        continue
                    self._set_state(first)
                    self._ws = ws
                    self.status = "connected"
                    for raw in ws:
                        self._on_message(json.loads(raw))
            except Exception as e:
                logger.info(f"relay connection lost: {e}")
            finally:
                self._ws = None
                if self.status in ("connected", "connecting"):
                    self.status = "offline"
                self._fail_waiters()
            self._stop.wait(self.RECONNECT_SECONDS)

    def _set_state(self, msg):
        with self._lock:
            self._new_state = (msg["rev"], msg["slots"])
            self._current = self._new_state

    def _on_message(self, msg):
        kind = msg.get("t")
        if kind == "state":
            self._set_state(msg)
        elif kind in ("ok", "deny"):
            with self._lock:
                waiter = self._waiters.get(msg.get("id"))
            if waiter is not None:
                waiter[1] = msg
                waiter[0].set()
        elif kind == "commit_rejected":
            logger.error(f"relay rejected commit {msg.get('id')}; its hold had expired")

    def _fail_waiters(self):
        with self._lock:
            for waiter in self._waiters.values():
                waiter[0].set()

    def request(self, expect, kind, timeout=1.5):
        ws = self._ws
        if ws is None or not self.connected:
            return "offline", None
        rid = uuid.uuid4().hex
        waiter = [threading.Event(), None]
        with self._lock:
            self._waiters[rid] = waiter
        start = time.monotonic()
        try:
            ws.send(json.dumps({"t": "request", "id": rid, "kind": kind, "expect": expect}))
            if not waiter[0].wait(timeout):
                return "timeout", rid
        except Exception:
            return "offline", None
        finally:
            with self._lock:
                self._waiters.pop(rid, None)
        if waiter[1] is None:
            return "offline", None
        self.last_rtt_ms = (time.monotonic() - start) * 1000
        return ("ok" if waiter[1]["t"] == "ok" else "deny"), rid

    def _send(self, msg):
        ws = self._ws
        if ws is None:
            logger.error(f"not connected, dropped {msg['t']}")
            return
        try:
            ws.send(json.dumps(msg))
        except Exception as e:
            logger.error(f"send {msg['t']} failed: {e}")

    def commit(self, rid, slots):
        self._send({"t": "commit", "id": rid, "slots": slots})

    def adjust(self, deltas, items):
        self._send({"t": "adjust", "deltas": deltas, "items": items})

    def take_state(self):
        with self._lock:
            state, self._new_state = self._new_state, None
        return state

    def current_state(self):
        with self._lock:
            return self._current
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest mod/tests -q && .venv/bin/pytest relay/tests -q`
Expected: `12 passed` for mod (6 logic + 6 client), `21 passed` for relay.

- [ ] **Step 5: Commit**

```bash
git add mod/sharedbox_lib/sync_client.py mod/tests/conftest.py mod/tests/test_sync_client.py
git commit -m "feat(mod): background relay client with approvals and reconnect"
```

---

### Task 6: Game layer, mod entry, install

**Files:**
- Create: `mod/sharedbox_lib/game_box.py`, `mod/shared_box.py`, `mod/shared_box.toml.example`, `mod/start-nms-shared.bat`, `tools/install_mod.sh`, `tools/fake_player.py`
- Move: `/mnt/c/Users/computer/NMSpyMods/shared_box_spike.py` → `spike/shared_box_spike.py` (throwaway reference; it hooks the same function and **must not** stay in the mods folder)

**Interfaces:**
- Consumes: Task 4 `slot_key`, `expect_for`, `deltas`, `classify`, `LocalState`; Task 5 `SyncClient`.
- Produces: `read_box(store) -> dict`, `write_box(store, slots) -> bool`, `ensure_capacity(vec, needed) -> bool`; the pyMHF mod `SharedBox`.

No automated tests: this code only runs inside the game. Verification is Step 5 plus Task 7.

- [ ] **Step 1: `mod/sharedbox_lib/game_box.py`**

```python
"""Read and write the shared box in game memory. Game thread only; needs nmspy (runs inside NMS)."""
import ctypes
import logging
import math

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


def ensure_capacity(vec, needed):
    """Grow an nmspy tk_vector to hold `needed` elements. nmspy's expand() grows by 1.5x the current
    length, which is 0 for an empty list, so we allocate ourselves with the same game allocator."""
    if vec.allocated_size >= needed:
        return True
    size = vec._template_type_size
    new_len = max(needed, math.ceil(vec.allocated_size * 1.5), 8)
    mm = engine_modules.gMemoryManager
    new_addr = mm.Malloc(new_len * size, 0, 0, 0, 16, -1)
    if not new_addr:
        logger.error("could not allocate memory to grow the box")
        return False
    old_addr = get_addressof(vec._ptr) if vec.allocated_size else 0
    if vec.vector_size:
        ctypes.memmove(int(new_addr), old_addr, size * vec.vector_size)
    if old_addr:
        mm.Free(ctypes.c_void_p(old_addr), -1)
    vec._ptr = ctypes.cast(int(new_addr), ctypes.POINTER(vec._template_type))
    vec.allocated_size = new_len
    return True


def write_box(store, target):
    """Make the in-game box match `target` exactly. Returns False if it couldn't."""
    vec = store.mStore
    current = {slot_key(vec[i].Index.X, vec[i].Index.Y): i for i in range(len(vec))}
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
    new_keys = []
    for key in target:
        if key in current:
            continue
        x, y = (int(v) for v in key.split(","))
        if x >= store.miWidth or y >= store.miHeight or not store.mxValidSlots[y][x]:
            logger.error(f"relay slot {key} does not exist in this box; skipped")
            continue
        new_keys.append(key)
    if new_keys and not ensure_capacity(vec, len(vec) + len(new_keys)):
        return False
    for key in new_keys:
        if not vec.append(_element(key, target[key])):
            return False
    return True
```

- [ ] **Step 2: `mod/shared_box.py`**

```python
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
from sharedbox_lib.game_box import read_box, write_box  # noqa: E402
from sharedbox_lib.sync_client import SyncClient  # noqa: E402

logger = logging.getLogger("SharedBox")

# cGcPlayerState menu "move item" (was NMS.exe+0x580000 on nmspy 180132). Every menu move runs through it.
MOVE_PATTERN = "4C 89 4C 24 20 4C 89 44 24 18 48 89 54 24 10 55 56 41 55"
MOVE_HOOK_NAME = "SharedBox_MoveItem"
FRAME_SECONDS = 0.1
WATCH_SECONDS = 0.25


def load_config():
    with open(os.path.join(HERE, "shared_box.toml"), "rb") as f:
        return tomllib.load(f)


@dataclass
class SharedBoxState(ModState):
    status: str = "starting"


class SharedBox(Mod):
    __author__ = "yiangwow"
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
        self.hello_cache = ({}, None, 10, 6)
        self.in_game = False
        self.needs_resync = False
        self.disabled_reason = None
        self._next_frame = 0.0
        self._next_watch = 0.0

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

    def _move_hook(self):
        return next((h for h in hook_manager.hooks.values() if getattr(h, "_name", None) == MOVE_HOOK_NAME), None)

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
        return ctypes.c_int32.from_address(p).value, ctypes.c_int32.from_address(p + 4).value

    # --- game state: loading a save means the relay's box wins, nothing is reported -------

    @nms.cTkFSMState.StateChange.after
    def on_state_change(self, this, lNewStateID, lpUserData, lbForceRestart):
        try:
            self.in_game = str(lNewStateID.contents) == "APPVIEW"
            self.baseline = None
            self.needs_resync = True
        except Exception:
            logger.error(f"state change handling failed:\n{traceback.format_exc()}")

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
            hook = self._move_hook()
            if hook is None or getattr(hook, "state", None) != "enabled":
                self.disabled_reason = "move hook not installed (game update?)"
                logger.error(self.disabled_reason)
                return
            self.hello_cache = (read_box(store), self.local_state.last_rev, store.miWidth, store.miHeight)
            self.client = SyncClient(self.cfg["relay_url"], self.cfg["token"], self.cfg["player"],
                                     lambda: self.hello_cache)
            self.client.start()
        rtt = f" | last reply {self.client.last_rtt_ms:.0f} ms" if self.client.last_rtt_ms else ""
        self.state.status = f"{self.client.status}{rtt}"

        # 1. Report changes made outside the menu (building/crafting from storage) before applying news.
        if self.baseline is not None and now >= self._next_watch:
            self._next_watch = now + WATCH_SECONDS
            local = read_box(store)
            self.hello_cache = (local, self.local_state.last_rev, store.miWidth, store.miHeight)
            if local != self.baseline and self.client.connected:
                changed, items = deltas(self.baseline, local)
                if changed:
                    logger.warning(f"box changed outside the menu, reporting {changed}")
                    self.client.adjust(changed, items)
                self.baseline = local

        # 2. Apply the relay's box: new states, or the latest one after a save load.
        new = self.client.take_state()
        if new is None and self.needs_resync:
            new = self.client.current_state()
        if new is not None:
            rev, slots = new
            if read_box(store) != slots and not write_box(store, slots):
                self.disabled_reason = "could not write the box"
                logger.error(self.disabled_reason)
                return
            self.baseline = slots
            self.needs_resync = False
            self.local_state.save(rev)

    # --- the move hook: ask the relay before any menu move that touches the box ----------

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
        original = self._move_hook().original
        try:
            return self._move(original, this, src, dst, src_slot, dst_slot, amount)
        except Exception:
            logger.error(f"move hook failed, running the game's move:\n{traceback.format_exc()}")
            return original(this, src, dst, src_slot, dst_slot, amount)

    def _move(self, original, this, src, dst, src_slot, dst_slot, amount):
        kind = classify(self._ref_index(src), self._ref_index(dst), self.box_index)
        if kind is None:
            return original(this, src, dst, src_slot, dst_slot, amount)
        store = self._store()
        if self.disabled_reason or self.client is None or not self.client.connected or self.baseline is None:
            logger.info(f"box locked ({self.state.status}); {kind} refused")
            return 0
        local = read_box(store)
        keys = []
        if kind in ("take", "move"):
            keys.append(slot_key(*self._slot(src_slot)))
        if kind in ("put", "move"):
            keys.append(slot_key(*self._slot(dst_slot)))
        outcome, rid = self.client.request(expect_for(local, keys), kind)
        if outcome != "ok":
            if outcome == "timeout":
                self.client.commit(rid, local)  # nothing moved; release the relay's hold now
            logger.info(f"{kind} refused ({outcome})")
            return 0
        result = original(this, src, dst, src_slot, dst_slot, amount)
        after = read_box(store)
        self.client.commit(rid, after)
        self.baseline = after
        logger.info(f"{kind} x{result} done in {self.client.last_rtt_ms:.0f} ms")
        return result
```

- [ ] **Step 3: Config, launcher, tools**

`mod/shared_box.toml.example`:
```toml
# Copy to shared_box.toml next to shared_box.py and fill in.
relay_url = "wss://nms.xyyoutube.com"
token = "PASTE_THE_TOKEN_HERE"
player = "your-name"
box_inventory_index = 12  # 12 = Container 0. Both players must use the same one.
```

`mod/start-nms-shared.bat`:
```bat
@echo off
rem Starts No Man's Sky with NMS.py and the shared box mod.
"%LOCALAPPDATA%\Programs\Python\Python311\Scripts\pymhf.exe" run nmspy
pause
```

`tools/install_mod.sh`:
```bash
#!/usr/bin/env bash
# Copy the shared box mod into the NMS.py mods folder on this PC and install its dependency.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODS="${NMS_MODS_DIR:-/mnt/c/Users/computer/NMSpyMods}"
PY="${NMS_PYTHON:-/mnt/c/Users/computer/AppData/Local/Programs/Python/Python311/python.exe}"
if [ -f "$MODS/shared_box_spike.py" ]; then
  mkdir -p "$REPO/spike"
  mv "$MODS/shared_box_spike.py" "$REPO/spike/"
  echo "Moved the spike mod out of $MODS (it hooks the same function)."
fi
mkdir -p "$MODS/_sharedbox/sharedbox_lib"
cp "$REPO/mod/shared_box.py" "$REPO/mod/start-nms-shared.bat" "$MODS/"
cp "$REPO/mod/sharedbox_lib/"*.py "$MODS/_sharedbox/sharedbox_lib/"
[ -f "$MODS/shared_box.toml" ] || cp "$REPO/mod/shared_box.toml.example" "$MODS/shared_box.toml"
"$PY" -m pip install --quiet websockets==13.1
echo "Installed into $MODS. Fill in shared_box.toml if you haven't."
```

`tools/fake_player.py`:
```python
"""Act as a second player: take AMOUNT items from SLOT of the shared box, through the relay.
Usage: python tools/fake_player.py URL TOKEN SLOT AMOUNT   (SLOT like 0,0)"""
import json
import sys
import uuid

from websockets.sync.client import connect

url, token, slot, amount = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])


def recv_until(ws, kind, rid=None):
    while True:
        msg = json.loads(ws.recv(timeout=10))
        if msg["t"] == kind and (rid is None or msg.get("id") == rid):
            return msg
        if msg["t"] == "deny" and msg.get("id") == rid:
            sys.exit(f"denied: {msg['reason']}")


with connect(url, open_timeout=10) as ws:
    ws.send(json.dumps({"t": "hello", "token": token, "player": "fake", "last_rev": 0, "slots": {}}))
    slots = recv_until(ws, "state")["slots"]
    if slot not in slots:
        sys.exit(f"slot {slot} is empty")
    stack = slots[slot]
    rid = uuid.uuid4().hex
    ws.send(json.dumps({"t": "request", "id": rid, "kind": "take",
                        "expect": {slot: {"id": stack["id"], "amount": stack["amount"]}}}))
    recv_until(ws, "ok", rid)
    new = dict(slots)
    left = stack["amount"] - amount
    if left > 0:
        new[slot] = dict(stack, amount=left)
    else:
        del new[slot]
    ws.send(json.dumps({"t": "commit", "id": rid, "slots": new}))
    state = recv_until(ws, "state")
    print(f"took {min(amount, stack['amount'])} {stack['id']} from {slot}; relay rev {state['rev']}")
```

- [ ] **Step 4: Syntax check and install**

```bash
cd ~/nms-shared-box && chmod +x tools/install_mod.sh
.venv/bin/python -m py_compile mod/shared_box.py mod/sharedbox_lib/game_box.py && echo SYNTAX_OK
tools/install_mod.sh
```
Expected: `SYNTAX_OK`, then `Installed into /mnt/c/Users/computer/NMSpyMods...`. Then write the real `token`, `player = "player1"` and `relay_url` into `/mnt/c/Users/computer/NMSpyMods/shared_box.toml` (token from Task 3 Step 5).

- [ ] **Step 5: Smoke check in game (user)**

Ask the user to double-click `C:\Users\computer\NMSpyMods\start-nms-shared.bat`, load their save, and look at the NMS.py window. Then:
```bash
L="/mnt/c/Program Files (x86)/Steam/steamapps/common/No Man's Sky/Binaries/logs"; grep -E "SharedBox|ERROR|Loaded" "$L/$(ls -t "$L" | head -1)" | tail -20
ssh tt@192.168.1.9 'docker logs --tail 5 nms-relay'
```
Expected: the mod loads without errors, the status shows `connected`, and the relay log shows `player1 seeded the box with N stacks`, where N matches Container 0. If the status shows `disabled: move hook not installed`, stop: the pattern needs re-deriving (see memory `nms-shared-box`).

- [ ] **Step 6: Commit**

```bash
git add mod/sharedbox_lib/game_box.py mod/shared_box.py mod/shared_box.toml.example mod/start-nms-shared.bat tools/install_mod.sh tools/fake_player.py spike/shared_box_spike.py
git commit -m "feat(mod): move hook, game-memory box sync and installer"
```

---

### Task 7: Solo in-game checklist

**Files:**
- Create: `docs/IN_GAME_CHECKLIST.md`

**Interfaces:**
- Consumes: the installed mod (Task 6) and the live relay (Task 3), plus `tools/fake_player.py` as the stand-in second player.

- [ ] **Step 1: Write `docs/IN_GAME_CHECKLIST.md`**

```markdown
# In-game checklist (solo)

Launch with `start-nms-shared.bat`, load your save, and keep the NMS.py window visible.
`FAKE` = `.venv/bin/python tools/fake_player.py wss://nms.xyyoutube.com <token>` (run from WSL in ~/nms-shared-box).

| # | Do | Expect |
|---|----|--------|
| A | Look at the NMS.py window | Status `connected` |
| B | Take a stack from Container 0 | Works; status shows `last reply N ms` |
| C | Put a stack into Container 0 | Works |
| D | Move a stack between two slots inside Container 0 | Works |
| E | `ssh tt@192.168.1.9 'docker stop nms-relay'`, try B and C | Both refused; status `offline`. `docker start nms-relay`; within ~5 s status is `connected` and B works |
| F | Save, quit, relaunch | Container 0 unchanged; relay log says `adopt` |
| G | Take 5 of something, then reload the last save from the menu | Box shows the relay's contents (the 5 are gone from the box); relay log has **no** `adjust` line |
| H | Fill your exosuit, then take a big stack | Only what fits moves; reopen Container 0: the rest is still there; `FAKE <slot> 0` prints the same amount |
| I | Close Container 0. Run `FAKE 0,0 1` | Within ~1 s, reopening Container 0 shows one fewer in slot 0,0 |
| J | **Keep Container 0 open** and run `FAKE 0,0 1` | Note what the screen does. Close and reopen it: the count must be right and nothing duplicated |
| K | Pick up a stack from Container 0 (on the cursor), and while holding it run `FAKE <that slot> 1` | Note what happens; after putting it down and reopening, nothing is duplicated |
| L | Build something that uses a material stored in Container 0 | Relay log shows an `adjust` with the used amount; box count right |

Log results in the table below with date and pass/fail. Any fail in G, H, J, K or L blocks Task 8.
```

- [ ] **Step 2: Run it with the user**

Go through A–L with the user, reading the pymhf log and `docker logs nms-relay` after each row. Record results under the table. A failure in J or K (a write while the box is open) goes back to design: the fallback is to skip writes while the box UI is open and apply them when it closes. Stop and flag it; don't patch it ad hoc.

- [ ] **Step 3: Commit**

```bash
git add docs/IN_GAME_CHECKLIST.md
git commit -m "docs: solo in-game checklist with results"
```

---

### Task 8: Friend setup, speed check, race check

**Files:**
- Create: `docs/FRIEND_SETUP.md`
- Modify: `CLAUDE.md` (status line), `docs/IN_GAME_CHECKLIST.md` (results)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write `docs/FRIEND_SETUP.md`**

```markdown
# Shared box: setup for a friend

1. Install **Python 3.11** from https://www.python.org/downloads/ (not 3.14, not the Microsoft Store one). Tick "Add python.exe to PATH".
2. Open PowerShell and run: `py -3.11 -m pip install nmspy websockets==13.1`
3. Make a folder `C:\NMSpyMods`. Unzip the folder you were sent into it, so it has `shared_box.py`, `shared_box.toml`, `start-nms-shared.bat` and `_sharedbox\`.
4. In `shared_box.toml`, set `player` to your name. Leave the token as sent.
5. Double-click `start-nms-shared.bat`. The first time, a window asks for the mods folder: choose `C:\NMSpyMods`.
6. **Before your first time: empty your Container 0.** The mod won't sync if it has items, so nobody's things get overwritten.
7. Always start the game with `start-nms-shared.bat`. If you play without it and touch Container 0, the shared box will overwrite those changes next time.

The NMS.py window shows `connected` when it's working. `offline` means the box is locked until the connection comes back.
```

- [ ] **Step 2: Package for the friend**

```bash
cd ~/nms-shared-box && grep -qx 'build/' .gitignore || echo 'build/' >> .gitignore
rm -rf build/friend-pkg && mkdir -p build/friend-pkg/_sharedbox/sharedbox_lib
cp mod/shared_box.py mod/start-nms-shared.bat docs/FRIEND_SETUP.md build/friend-pkg/
cp mod/sharedbox_lib/*.py build/friend-pkg/_sharedbox/sharedbox_lib/
TOKEN=$(ssh tt@192.168.1.9 "grep '^NMS_RELAY_TOKEN=' ~/is-infra/projects/self-hosted/.env | cut -d= -f2-")
sed "s|PASTE_THE_TOKEN_HERE|$TOKEN|; s|your-name|friend|" mod/shared_box.toml.example > build/friend-pkg/shared_box.toml
python3 -c "import shutil; shutil.make_archive('/mnt/c/Users/computer/Downloads/nms-shared-box-friend', 'zip', 'build/friend-pkg')"
ls -la /mnt/c/Users/computer/Downloads/nms-shared-box-friend.zip
```
Expected: the zip in Downloads. Tell the user it contains the relay token, so they should send it privately.

- [ ] **Step 3: Speed check (user + friend)**

Both players connected, each does 20 takes from Container 0. Collect the round-trip times:
```bash
L="/mnt/c/Program Files (x86)/Steam/steamapps/common/No Man's Sky/Binaries/logs"; grep -o "done in [0-9]* ms" "$L/$(ls -t "$L" | head -1)" | tail -20
```
The friend reads the same lines from their own `Binaries\logs`. **Gate:** both say the pause feels fine. Record the measured numbers and the verdict in `docs/IN_GAME_CHECKLIST.md`. If it fails, stop and flag it: the fallback (optimistic local takes, spec Option 2) is a new design decision.

- [ ] **Step 4: Race check (user + friend)**

Put one stack of 10 of something in Container 0. Count down and both take it at the same moment, 5 times (refill between rounds). **Expect:** each round exactly one player gets it, the other sees nothing move, and the totals across both exosuits plus the box stay 10. Record the results.

- [ ] **Step 5: Finish**

Update `CLAUDE.md` with one line under Deploy: `Status: live since <date>; speed check <median> ms / <p-worst> ms.` Commit, push the branch, open PR 2 and confirm it:
```bash
git add .gitignore docs/FRIEND_SETUP.md docs/IN_GAME_CHECKLIST.md CLAUDE.md
git commit -m "docs: friend setup and two-player check results"
git push -u origin feat/shared-box-mod
gh pr create --base main --head feat/shared-box-mod --title "Shared box: mod" --body "NMS.py mod for the shared box. Spec: docs/superpowers/specs/2026-09-30-nms-shared-box-design.md"
gh pr view --json state,url && gh pr diff --name-only
```
Expected: PR `OPEN`, and the diff lists exactly the files from Tasks 4–8.
