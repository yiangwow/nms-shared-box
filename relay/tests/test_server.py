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
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        assert (await recv_type(a, "ok"))["id"] == "r1"
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {"0,0": rec("FUEL1", 2)}}))
        state = await recv_type(b, "state")
        assert state["slots"]["0,0"]["amount"] == 2 and state["rev"] == 2
        await shutdown(srv)
    asyncio.run(run())


def test_box_file_already_updated_when_committed_arrives(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {"0,0": rec("FUEL1", 2)}}))
        ack = await recv_type(a, "committed")
        on_disk = json.loads((tmp_path / "box.json").read_text())
        assert on_disk["rev"] == ack["rev"] == 2
        assert on_disk["slots"]["0,0"]["amount"] == 2
        assert "r1" in on_disk["recent_commits"]
        await shutdown(srv)
    asyncio.run(run())


def test_second_request_while_pending_is_busy(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        expect = {"0,0": {"id": "FUEL1", "amount": 5}}
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1, "expect": expect}))
        await recv_type(a, "ok")
        await b.send(json.dumps({"t": "request", "id": "r2", "kind": "take", "base_rev": 1, "expect": expect}))
        assert (await recv_type(b, "deny"))["reason"] == "busy"
        await shutdown(srv)
    asyncio.run(run())


def test_stale_expect_denied_after_commit(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        expect = {"0,0": {"id": "FUEL1", "amount": 5}}
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1, "expect": expect}))
        await recv_type(a, "ok")
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {}}))
        await recv_type(b, "state")
        await b.send(json.dumps({"t": "request", "id": "r2", "kind": "take", "base_rev": 2, "expect": expect}))
        assert (await recv_type(b, "deny"))["reason"] == "stale"
        await shutdown(srv)
    asyncio.run(run())


def test_request_with_old_base_rev_denied_stale_even_if_expect_matches(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5), "1,0": rec("LAND1", 2)}, player="a")
        b, _ = await hello(url, {}, player="b")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await a.send(json.dumps({"t": "commit", "id": "r1",
                                 "slots": {"1,0": rec("LAND1", 2)}}))
        await recv_type(b, "state")
        # b's box is one revision behind, but the slot it asks about is unchanged.
        await b.send(json.dumps({"t": "request", "id": "r2", "kind": "take", "base_rev": 1,
                                 "expect": {"1,0": {"id": "LAND1", "amount": 2}}}))
        assert (await recv_type(b, "deny"))["reason"] == "stale"
        await shutdown(srv)
    asyncio.run(run())


def test_request_without_base_rev_denied_stale(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take",
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        assert (await recv_type(a, "deny"))["reason"] == "stale"
        await shutdown(srv)
    asyncio.run(run())


def test_same_empty_slot_race(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        empty = {"4,4": None}
        await a.send(json.dumps({"t": "request", "id": "ra", "kind": "put", "base_rev": 1, "expect": empty}))
        assert (await recv_type(a, "ok"))["id"] == "ra"
        await b.send(json.dumps({"t": "request", "id": "rb", "kind": "put", "base_rev": 1, "expect": empty}))
        assert (await recv_type(b, "deny"))["reason"] == "busy"
        await a.send(json.dumps({"t": "commit", "id": "ra",
                                 "slots": {"0,0": rec("FUEL1", 5), "4,4": rec("LAND1", 3)}}))
        await recv_type(b, "state")
        await b.send(json.dumps({"t": "request", "id": "rb2", "kind": "put", "base_rev": 2, "expect": empty}))
        assert (await recv_type(b, "deny"))["reason"] == "stale"
        await shutdown(srv)
    asyncio.run(run())


def test_commit_records_actual_snapshot(tmp_path):
    # Taking into a full exosuit moves fewer than asked; the commit snapshot is what counts.
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 50)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
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
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
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
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1, "expect": expect}))
        await recv_type(a, "ok")
        await asyncio.sleep(0.2)
        await b.send(json.dumps({"t": "request", "id": "r2", "kind": "take", "base_rev": 1, "expect": expect}))
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


def test_commit_from_new_connection_accepted_while_hold_open(tmp_path):
    # The mover's connection dropped after "ok"; it reconnects inside the hold and resends the commit.
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await a.close()
        a2, _ = await hello(url, {}, last_rev=1, player="a")
        await a2.send(json.dumps({"t": "commit", "id": "r1", "slots": {"0,0": rec("FUEL1", 2)}}))
        assert (await recv_type(a2, "committed"))["id"] == "r1"
        state = await recv_type(a2, "state")
        assert state["rev"] == 2 and state["slots"]["0,0"]["amount"] == 2
        assert relay.pending is None
        await shutdown(srv)
    asyncio.run(run())


def test_commit_from_new_connection_rejected_after_hold_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(relay_server, "PENDING_SECONDS", 0.1)

    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await a.close()
        await asyncio.sleep(0.2)
        a2, _ = await hello(url, {}, last_rev=1, player="a")
        await a2.send(json.dumps({"t": "commit", "id": "r1", "slots": {"0,0": rec("FUEL1", 2)}}))
        assert (await recv_type(a2, "commit_rejected"))["id"] == "r1"
        assert relay.box.slots["0,0"]["amount"] == 5 and relay.box.rev == 1
        await shutdown(srv)
    asyncio.run(run())


def test_commit_with_other_id_rejected_while_hold_open(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await b.send(json.dumps({"t": "commit", "id": "other", "slots": {}}))
        assert (await recv_type(b, "commit_rejected"))["id"] == "other"
        assert relay.pending is not None and relay.box.slots["0,0"]["amount"] == 5
        await shutdown(srv)
    asyncio.run(run())


def test_resent_commit_already_applied_is_confirmed_not_reapplied(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {"0,0": rec("FUEL1", 2)}}))
        await recv_type(a, "state")
        await a.send(json.dumps({"t": "adjust", "deltas": {"FUEL1": 1}, "items": {}}))
        await recv_type(a, "state")
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {"0,0": rec("FUEL1", 2)}}))
        assert (await recv_type(a, "committed"))["id"] == "r1"
        assert relay.box.rev == 3 and relay.box.slots["0,0"]["amount"] == 3
        await shutdown(srv)
    asyncio.run(run())


def test_recent_commits_survive_restart(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await a.send(json.dumps({"t": "commit", "id": "r1", "slots": {}}))
        await recv_type(a, "state")
        await shutdown(srv)
        relay2, srv2, url2 = await boot(tmp_path)
        a2, _ = await hello(url2, {}, last_rev=2, player="a")
        await a2.send(json.dumps({"t": "commit", "id": "r1", "slots": {}}))
        assert (await recv_type(a2, "committed"))["id"] == "r1"
        await shutdown(srv2)
    asyncio.run(run())


def test_adjust_during_hold_survives_holders_commit(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5), "1,0": rec("LAND1", 4)}, player="a")
        b, _ = await hello(url, {}, player="b")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        # b crafts with 3 LAND1 from the box while a's take is open
        await b.send(json.dumps({"t": "adjust", "deltas": {"LAND1": -3}, "items": {}}))
        await asyncio.sleep(0.1)
        assert relay.box.slots["1,0"]["amount"] == 4  # queued, not applied yet
        await a.send(json.dumps({"t": "commit", "id": "r1",
                                 "slots": {"0,0": rec("FUEL1", 1), "1,0": rec("LAND1", 4)}}))
        state = await recv_type(b, "state")
        assert state["slots"]["0,0"]["amount"] == 1 and state["slots"]["1,0"]["amount"] == 1
        assert state["rev"] == 3 and relay.queued_adjusts == []
        await shutdown(srv)
    asyncio.run(run())


def test_adjust_during_hold_applied_when_hold_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(relay_server, "PENDING_SECONDS", 0.1)

    async def run():
        relay, srv, url = await boot(tmp_path)
        a, _ = await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        b, _ = await hello(url, {}, player="b")
        await a.send(json.dumps({"t": "request", "id": "r1", "kind": "take", "base_rev": 1,
                                 "expect": {"0,0": {"id": "FUEL1", "amount": 5}}}))
        await recv_type(a, "ok")
        await b.send(json.dumps({"t": "adjust", "deltas": {"FUEL1": -2}, "items": {}}))
        # no further message: the hold's own timer must release it and apply the adjust
        state = await recv_type(b, "state")
        assert state["slots"]["0,0"]["amount"] == 3 and relay.pending is None
        await shutdown(srv)
    asyncio.run(run())


def test_seed_stores_valid_slots_and_adjust_uses_only_them(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        ws = await connect(url)
        await ws.send(json.dumps({"t": "hello", "token": "t", "player": "a", "last_rev": None,
                                  "slots": {"0,0": rec("FUEL1", 5)}, "width": 10, "height": 6,
                                  "valid_slots": ["0,0", "3,2"]}))
        await recv_type(ws, "state")
        await ws.send(json.dumps({"t": "adjust", "deltas": {"GOLD": 7}, "items": {"GOLD": rec("GOLD", 7)}}))
        state = await recv_type(ws, "state")
        assert set(state["slots"]) == {"0,0", "3,2"}
        await shutdown(srv)
        relay2, srv2, _ = await boot(tmp_path)
        assert relay2.box.valid_slots == ["0,0", "3,2"]
        await shutdown(srv2)
    asyncio.run(run())


def test_later_hello_narrows_valid_slots(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        await hello(url, {"0,0": rec("FUEL1", 5)}, player="a")
        assert relay.box.valid_slots is None  # seeded by a client that didn't send them
        ws = await connect(url)
        await ws.send(json.dumps({"t": "hello", "token": "t", "player": "b", "last_rev": 1, "slots": {},
                                  "width": 10, "height": 6, "valid_slots": ["0,0", "1,0", "2,0"]}))
        await recv_type(ws, "state")
        assert relay.box.valid_slots == ["0,0", "1,0", "2,0"]
        ws2 = await connect(url)
        await ws2.send(json.dumps({"t": "hello", "token": "t", "player": "c", "last_rev": 1, "slots": {},
                                   "width": 10, "height": 6, "valid_slots": ["1,0", "2,0", "9,5"]}))
        await recv_type(ws2, "state")
        assert relay.box.valid_slots == ["1,0", "2,0"]
        await shutdown(srv)
    asyncio.run(run())


def _raw_hello(player, last_rev, valid, slots=None):
    msg = {"t": "hello", "token": "t", "player": player, "last_rev": last_rev,
           "slots": slots or {}, "width": 10, "height": 6}
    if valid is not None:
        msg["valid_slots"] = valid
    return json.dumps(msg)


def test_empty_or_missing_valid_slots_ignored(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        ws = await connect(url)
        await ws.send(_raw_hello("a", None, ["0,0", "1,0"], {"0,0": rec("FUEL1", 5)}))
        await recv_type(ws, "state")
        for valid in ([], None):
            w = await connect(url)
            await w.send(_raw_hello("b", 1, valid))
            await recv_type(w, "state")
            assert relay.box.valid_slots == ["0,0", "1,0"]
        await shutdown(srv)
    asyncio.run(run())


def test_reseed_replaces_valid_slots(tmp_path):
    async def run():
        relay, srv, url = await boot(tmp_path)
        relay.box.valid_slots = ["0,0"]  # stale narrow set, box not yet seeded
        ws = await connect(url)
        await ws.send(_raw_hello("a", None, ["0,0", "1,0", "2,0"], {"0,0": rec("FUEL1", 5)}))
        await recv_type(ws, "state")
        assert relay.box.valid_slots == ["0,0", "1,0", "2,0"]
        await shutdown(srv)
    asyncio.run(run())


def test_main_refuses_empty_token(monkeypatch):
    import pytest

    monkeypatch.setenv("NMS_RELAY_TOKEN", "")
    with pytest.raises(SystemExit):
        asyncio.run(relay_server.main())
    monkeypatch.delenv("NMS_RELAY_TOKEN")
    with pytest.raises(SystemExit):
        asyncio.run(relay_server.main())
