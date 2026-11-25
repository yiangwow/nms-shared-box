# NMS Shared Box — Design

Date: 2026-09-30 · Status: draft, awaiting review

## Contents
1. Goal
2. What the spikes proved
3. How it works
4. The relay
5. The mod
6. Failure rules
7. Setup and install
8. Testing and the speed check
9. Out of scope
10. Open risks

## 1. Goal
One storage container in No Man's Sky that two PC players share. Both see the same contents.
Syncing is automatic, with no buttons. It must never duplicate or lose items through a race.
It works whether the players are in the same session or playing solo at different times.

## 2. What the spikes proved (2026-09-30, NMS Cosmos, nmspy 180132)
- NMS.py can read and write `player_state.mInventories` live. `mInventories[12]` is Container 0.
- Changing a stack's amount, and appending a stack into spare capacity, both persist through save + reload (checked in the decoded save file).
- Every menu move goes through `NMS.exe+0x580000` ("move item"). Its arguments are the source and destination inventory index, both slots and the amount. It returns the amount moved.
- A `@NOOP` hook on it that returns 0 cleanly cancels a move. Allowed moves are passed to the original and behave normally.
- X quick-transfer does not use that function; it has its own (pattern `48 89 5C 24 18 55 57 41 57 48 81 EC 80 00 00 00`, NMS build 25625620: rcx = source store, rdx = destination ref, r8/r9 = slots, stack = element, amount). Both are hooked.
- Menu moves never call `cGcInventoryStore::Remove`. Partial moves write the amount inline, so `0x580000` is the only hook point.

## 3. How it works
The relay holds the true box. Every menu change to the box is asked for first and approved or refused by the relay, one request at a time.

```
drag touching the box ─► hook pauses the move ─► request to relay ─► relay checks the true box
                                                                     ├─ ok:   move runs, relay broadcasts new box
                                                                     └─ deny: hook returns 0, nothing moves
other player's mod ◄──── new box (within ~1 s) ◄─ broadcast
```

Three kinds of request, all made from the `0x580000` hook:
- **take**: box → elsewhere. Relay checks that the slot holds that item and at least that amount.
- **put**: elsewhere → box. Relay checks that the target slot is empty or holds the same item with room.
- **move**: box → box. Relay checks the source slot as for a take.

After an approved move, the mod reads the box and sends its new contents with the request id, so the relay records exactly what the game did.

**Incoming updates:** the mod writes the relay's box into the local box on the game thread.

**Safety net (watcher):** every 250 ms the mod compares the box to the last relay state. A difference it did not request (for example building or crafting using materials from storage) is sent as an **adjust** (per-item deltas). The relay applies it, clamped at zero, and logs any shortfall.

## 4. The relay
- New repo `~/nms-shared-box`, folder `relay/`: Python 3.12, one WebSocket endpoint, JSON messages.
- Runs as container `nms-relay` in `is-infra`'s docker-compose. No published port. It's reached only through the existing Cloudflare tunnel at `nms.xyyoutube.com`.
- Requests are handled strictly in order (one lock). Each change bumps a revision number.
- State is saved to `/data/box.json` (atomic write) after every change, so a restart loses nothing.
- Auth: a shared token in the first message. A wrong token closes the connection.
- Messages: `hello{token, player, last_rev, slots, width, height, valid_slots}` → `state{rev, slots}`; `request{id, kind, base_rev, expect}` → `ok{id}` / `deny{id, reason}` (the relay denies "stale" when `base_rev` is missing or not the current revision, so a client whose box hasn't applied the latest update can't commit over it); `commit{id, slots}` → `committed{id, rev}` or `commit_rejected{id}`; `adjust{deltas, items}`; broadcast `state{rev, slots}`.
- A commit is matched to the open request by its **id**, not the connection: a mod that reconnects inside the 10 s hold resends it. The relay remembers recent commit ids, so a resend of an applied commit is confirmed and not applied twice. A rejected commit (hold expired) is reported by the mod as an `adjust`, so the move is recorded instead of undone.
- Adjusts that arrive during another player's hold are queued and applied after that commit (or the hold's expiry), so the whole-box replace can't erase them.
- `valid_slots` (the box's real slots) limits where the relay places new stacks.
- A slot holds the full item record: `id, amount, max_amount, type, damage_factor, added_automatically, fully_installed`.

## 5. The mod
- One file, `mod/shared_box.py`, in the NMS.py mods folder, plus `shared_box.toml` (relay URL, token, player name, box inventory index, default 12 = Container 0).
- Functions are found by **byte pattern**, not fixed address. If a pattern is missing or not unique, the mod disables itself and logs why.
- The network client runs on a background thread. The hook waits for a reply for at most **1.5 s**. No reply counts as deny.
- Writing the box: update amounts in place and append into spare capacity. When capacity runs out it needs its own vector grow, because nmspy's `expand()` is broken (it calls a missing `add`).
- **First connect:** if the relay box is empty, it's seeded from this player's box. If the relay box is not empty and this player's box is not empty, the mod refuses to sync and says "empty Container 0 first", so no one's items get overwritten.
- The relay client starts only after the first save load, so first-connect judges the player's real box. A refused player may take items out of the box (it isn't shared for them yet) and is retried every ~10 s.
- Status is visible in the NMS.py window: connected / locked / last error.

## 6. Failure rules
| Situation | Behaviour |
|---|---|
| Relay unreachable or timed out | Box locked: every move touching it is refused. Unlocks on reconnect. |
| Two players take the same stack | Relay approves the first, denies the second. |
| Change not made through the menu (watcher) | Sent as adjust, clamped at zero; a shortfall is logged. This is the one remaining race. |
| Mod bug inside the hook | Run the game's original move (spike behaviour). |
| Game update breaks a pattern | Mod disables itself. Box stays as it is, unsynced. |

## 7. Setup and install
- **Server (once):** add `nms-relay` to `is-infra`, set `NMS_RELAY_TOKEN` in the server `.env`, and add the public hostname `nms.xyyoutube.com → http://nms-relay:8080` in the Cloudflare dashboard (the tunnel is token-managed).
- **Each player:** Python 3.11 from python.org, `py -3.11 -m pip install nmspy websockets==13.1`, copy the mod folder, and launch with `start-nms-shared.bat` (wraps `pymhf.exe run nmspy`).

## 8. Testing and the speed check
- **Relay:** pytest on request handling: take/put/move rules, ordering, clamped adjust, revision numbers, save/load.
- **Mod logic:** pytest on the pure parts (box diff, applying a state to a fake inventory, first-connect rule) without the game.
- **In game (you, alone):** a checklist covering pass-through with the switch off, take/put/move while connected, lock when the relay is stopped, save + reload persistence.
- **Speed check (you + friend, required before calling it done):** the mod logs each request's round-trip time. Both of you do 20 takes. **Gate:** the pause must feel fine to both of you. If it doesn't, the fallback is optimistic local takes with the relay settling afterwards (spike Option 2).
- **Race check (you + friend):** both grab the same stack at the same moment, 5 times. Expect exactly one success each time and no duplicate.

## 9. Out of scope
Ship sharing (a copy via a save editor stays the manual fallback). More than one shared box. Console players. Sharing the corvette cargo.

## 10. Open risks
- Writing the box while the other player has it open on screen may not refresh the UI, or may conflict with a drag in progress. Test early.
- The capacity grow for new stacks is new code into game memory.
- Building/crafting may use the box in ways the watcher sees late (see §6).
- Container numbers other than 0 are not mapped yet (inventory 21 turned out to be corvette cargo 9).
