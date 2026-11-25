# In-game checklist (solo)

Launch with `start-nms-shared.bat`, load your save, and keep the NMS.py window visible.
`FAKE` = `.venv/bin/python tools/fake_player.py wss://nms.xyyoutube.com <token>` (run from WSL in ~/nms-shared-box).
`FAKE_PUT` = the same command with `put` inserted after the token, e.g. `... <token> put SLOT ID AMOUNT` (copies the record of an existing stack of that item).

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
| M | Close Container 0. Run `FAKE_PUT 5,5 HULK1 1` | Within ~1 s a new stack of 1 HULK1 appears in slot 5,5 when you reopen Container 0; the pymhf log shows a `grew the box` line the first time. Then reload your last save, then quit to the main menu: **the game must not crash** |

Log results in the table below with date and pass/fail. Any fail in G, H, J, K, L or M blocks Task 8.

## Results

| # | Date | Pass/Fail | Notes |
|---|------|-----------|-------|
| A | 2026-09-30 | PASS | Status `connected`; relay: `the owner seeded the box with 3 stacks` (matches Container 0) |
| B | 2026-09-30 | PASS | take x3 / x16 / x1 approved; 63, 93, 79 ms |
| C | 2026-09-30 | PASS | put x16 / x3 approved; 63, 47 ms |
| D | 2026-09-30 | PASS | 4 in-box moves approved; 63-94 ms |
| E | 2026-09-30 | PASS | Relay stopped: status offline, take/put refused. Restarted: reconnected (`adopt`) within ~0.1 s; box rev 10 kept |
| F | 2026-09-30 | PASS | Quit + relaunch: `connected (adopt)`, box unchanged, no adjust |
| G | 2026-09-30 | FAIL, then PASS | First run: the reloaded save's box was reported as a change (+16) — the save won. Fixed by hooking `cGcPlayerState::LoadFromData` (ca5ec0d). Retest: save had 16, relay 13 → box 13 after load, no adjust; a reload also kept a relay-only stack (row M) |
| H | 2026-09-30 | SKIPPED | Skipped at the user's request; covered by relay test `test_commit_records_actual_snapshot` |
| I | 2026-09-30 | PASS | Fake take 1 → box 16 → 15 |
| J | 2026-09-30 | PASS | Fake take while Container 0 open: the count updated live without closing; 14, nothing duplicated |
| K | 2026-09-30 | PASS | Fake take while the stack was on the cursor; after putting it down: 13, no duplication (a one-second visual blip) |
| L | 2026-09-30 | N/A | The build menu does not use materials in storage. This row exposed that **X quick-transfer** bypassed the move hook (changes arrived only as watcher adjusts). Fixed: second hook + revision-guarded requests (2f1a96e); 6 quick transfers then approved, 31-203 ms |
| M | 2026-09-30 | PASS | First try used a slot that doesn't exist (5,5): the mod refused to write and disabled itself (safe). Second: box grew 2 → 60; fake put 4,0; reload kept it; quit to main menu with no crash; a fake put made while in the main menu appeared on load |

Other observations:
- NMS auto-updated during testing (13:40, Steam build 25442159 → 25625620). Both hooks resolve by pattern on the new build; rows B-M ran on it.
- User PC had a ~1 min DNS blip (14:01); the mod locked the box and reconnected by itself.
- After each own move the next one is refused for ~0.1-0.2 s ("box updating"). User: "a bit annoying but not terrible, depends on what my friend says".
