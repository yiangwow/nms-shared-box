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
