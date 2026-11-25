# CLAUDE.md

Shared storage box for No Man's Sky (PC) between two players, built on NMS.py (pyMHF 0.2.4 + nmspy).
Spec (source of truth for behaviour): `docs/superpowers/specs/2026-09-30-nms-shared-box-design.md`.

## Contents
1. Layout
2. Install paths
3. Hooks
4. Protocol
5. Relay env and service
6. Friend package
7. Tests
8. Deploy

## 1. Layout
- `relay/` — WebSocket relay (Python 3.12, `websockets==13.1`): `box.py` (state + rules, no I/O), `server.py`, `Dockerfile`, `tests/`.
- `mod/` — `shared_box.py` (the pyMHF mod, Windows Python 3.11), `sharedbox_lib/` (`box_logic.py` pure helpers, `sync_client.py` network thread, `game_box.py` game memory read/write), `shared_box.toml.example`, `start-nms-shared.bat`, `install-friend.bat`, `tests/`.
- `tools/` — `install_mod.sh` (copy the mod to this PC), `relay_ping.py` (probe, no box change), `fake_player.py` (act as a second player).
- `docs/` — spec + plan (`superpowers/`), `FRIEND_SETUP.md`, `IN_GAME_CHECKLIST.md`. `spike/` — the old spike mod, not installed.

## 2. Install paths
- This PC: mods folder `C:\Users\computer\NMSpyMods` (`tools/install_mod.sh`, override `NMS_MODS_DIR` / `NMS_PYTHON`).
- Friend: `C:\NMSpyMods` (`install-friend.bat`, also writes `%APPDATA%\pymhf\nmspy\pymhf.local.toml`).
- Both: `shared_box.py`, `shared_box.toml`, `start-nms-shared.bat` in the mods folder; helpers in `_sharedbox\sharedbox_lib\` (two levels down so pyMHF doesn't load them as mods). `shared_box_state.json` (last synced rev) is written next to the mod.
- Logs: `<NMS>\Binaries\logs\pymhf-*.log`. Launch: `start-nms-shared.bat` (`pymhf.exe run nmspy`).

## 3. Hooks
Shared box: `box_inventory_index` in `shared_box.toml` = index into `player_state.mInventories`. Both players use **21 = corvette cargo box 9** (since 2026-09-30; 12 = base Container 0, used during testing).

All found by byte pattern (never fixed offsets); tested on Steam build 25625620. A missing move/quick hook disables the mod with a reason in the status.
- Menu move: `MOVE_PATTERN` in `shared_box.py` (NOOP hook, asks the relay first).
- X quick-transfer: `QUICK_PATTERN` (NOOP hook, asks the relay first).
- `cGcPlayerState.LoadFromData` (after): save loaded → relay box wins; the client starts only after the first one.
- `cGcApplication.Update` (after): the 100 ms tick on the game thread (apply relay states, watcher, resend).
- `cTkFSMState.StateChange` (after): tracks in-game (`APPVIEW`).
- Hook bodies go through `_run_hook`: always return an int, never run the game's original twice.

## 4. Protocol
JSON over one WebSocket, one lock, every change bumps `rev`.
- `hello{token, player, last_rev, slots, width, height, valid_slots}` → `state{rev, slots}` | `refused` | `error`. Seed / adopt / refuse rules: `box.decide_hello`.
- `request{id, kind, base_rev, expect}` → `ok` | `deny{reason: busy|stale}`. `stale` when `base_rev` isn't the current rev or `expect` doesn't match. An `ok` opens a 10 s hold.
- `commit{id, slots}` → `committed{id, rev}` + broadcast `state`, or `commit_rejected{id}`. Matched by request id (any connection), so a reconnected mod resends it; recent ids are remembered (persisted) so a resend is confirmed, not reapplied.
- `adjust{deltas, items}` (watcher, offline changes, rejected commits) → broadcast `state`. During a hold it's queued and applied after the commit or the hold's expiry.
- **Redeploy the relay before shipping a mod that needs a protocol change.**

## 5. Relay env and service
- `NMS_RELAY_TOKEN` (required; empty → refuses to start), `NMS_RELAY_DATA` (default `/data/box.json`), `NMS_RELAY_PORT` (default 8080).
- Container `nms-relay` in `~/is-infra/projects/self-hosted/docker-compose.yml`, volume `nms-relay-data:/data`, network `self-hosted`, **no published port**. Public only via the Cloudflare tunnel at `nms.xyyoutube.com` (hostname set in the Cloudflare dashboard).
- Token lives in the server `.env`; same value in each player's `shared_box.toml`. Never print it.
- Probe: `.venv/bin/python tools/relay_ping.py wss://nms.xyyoutube.com <token>`.

## 6. Friend package
- Build into `build/friend-pkg/` (gitignored): `shared_box.py`, `start-nms-shared.bat`, `install-friend.bat`, `FRIEND_SETUP.md`, `_sharedbox/sharedbox_lib/*.py`, `shared_box.toml` from the example.
- The token is injected into `build/friend-pkg/shared_box.toml` by hand, then zipped to Downloads. **The zip contains the secret: send it privately.**

## 7. Tests
- `.venv/bin/pytest relay/tests` and `.venv/bin/pytest mod/tests` (local venv: Python 3.10, websockets 13.1). The mod tests run a real relay in a thread.
- `shared_box.py` and `game_box.py` need the game; check them with `py_compile` and `docs/IN_GAME_CHECKLIST.md`.

## 8. Deploy
- Server clone: `~/nms-shared-box` on tt-server (compose builds from `/home/tt/nms-shared-box/relay`). It must be on `main` after merge — currently on `feat/shared-box-mod`.
- `git pull` the server clone, then `deploy` from `~/is-infra` (the deploy hook rebuilds the stack; works again as of 2026-09-30).
- Mod: `tools/install_mod.sh` on this PC; rebuild and resend the friend package.
