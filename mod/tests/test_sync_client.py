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
        outcome, rid = a.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take", a.current_state()[0])
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
        outcome, _ = a.request({"0,0": {"id": "FUEL1", "amount": 4}}, "take", a.current_state()[0])
        assert outcome == "deny"
    finally:
        a.stop()


def test_request_with_stale_base_rev_denied(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)})
    try:
        assert wait_for(lambda: a.connected)
        rev = a.current_state()[0]
        # expect matches, but the client claims a revision the relay has moved past
        outcome, _ = a.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take", rev - 1)
        assert outcome == "deny"
        assert a.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take", rev)[0] == "ok"
    finally:
        a.stop()


def test_refused_new_player_with_items(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)}, player="a")
    assert wait_for(lambda: a.connected)
    b = client(relay.url, {"1,1": rec("LAND1", 2)}, player="b")
    try:
        assert wait_for(lambda: b.status.startswith("refused"))
        assert b.request({}, "take", 0)[0] == "offline"
    finally:
        a.stop()
        b.stop()


def test_offline_when_no_relay():
    c = client("ws://127.0.0.1:9", {})
    try:
        time.sleep(0.3)
        assert not c.connected
        assert c.request({}, "take", 0)[0] == "offline"
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
        first_epoch = a.connection_epoch
        assert first_epoch >= 1
        r1.stop()
        assert wait_for(lambda: not a.connected)
        r2 = RelayThread(path, port=port)
        try:
            assert wait_for(lambda: a.connected, timeout=10)
            assert a.connection_epoch > first_epoch
        finally:
            r2.stop()
    finally:
        a.stop()


def fake_server(on_request):
    """Sync websockets server: answers hello with an empty state, then calls on_request(ws)."""
    import json
    import threading

    from websockets.sync.server import serve

    def handler(ws):
        ws.recv()
        ws.send(json.dumps({"t": "state", "rev": 1, "slots": {}}))
        try:
            for raw in ws:
                if json.loads(raw).get("t") == "request":
                    on_request(ws)
        except Exception:
            pass

    srv = serve(handler, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"ws://127.0.0.1:{srv.socket.getsockname()[1]}"


def test_request_times_out_when_server_silent():
    srv, url = fake_server(lambda ws: None)
    c = client(url, {})
    try:
        assert wait_for(lambda: c.connected)
        t0 = time.monotonic()
        outcome, rid = c.request({}, "take", 0, timeout=0.3)
        assert outcome == "timeout" and rid
        assert time.monotonic() - t0 < 1.0
    finally:
        c.stop()
        srv.shutdown()


def test_pending_request_offline_when_connection_drops():
    srv, url = fake_server(lambda ws: ws.close())
    c = client(url, {})
    c.RECONNECT_SECONDS = 30
    try:
        assert wait_for(lambda: c.connected)
        t0 = time.monotonic()
        outcome, rid = c.request({}, "take", 0, timeout=5)
        assert outcome == "offline" and rid is None
        assert time.monotonic() - t0 < 2.0
    finally:
        c.stop()
        srv.shutdown()


def test_stays_connected_across_idle_keepalive_intervals(relay):
    c = SyncClient(relay.url, "t", "p", lambda: ({"0,0": rec("FUEL1", 5)}, None, 10, 6))
    c.KEEPALIVE_SECONDS = 0.1
    c.start()
    try:
        assert wait_for(lambda: c.connected)
        time.sleep(0.8)
        assert c.connected
        assert c.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take", c.current_state()[0])[0] == "ok"
    finally:
        c.stop()


def test_stop_during_handshake_ends_connection(relay):
    import threading

    gate = threading.Event()

    def slow_hello():
        gate.wait(5)
        return ({"0,0": rec("FUEL1", 5)}, None, 10, 6)

    c = SyncClient(relay.url, "t", "p", slow_hello)
    c.start()
    time.sleep(0.2)  # inside hello_source, before _ws is set
    c.stop()
    gate.set()
    assert wait_for(lambda: not c._thread.is_alive(), timeout=3)
    assert not c.connected


def test_commit_rejected_after_hold_expires_is_reported(relay, monkeypatch):
    import server

    monkeypatch.setattr(server, "PENDING_SECONDS", 0.1)
    a = client(relay.url, {"0,0": rec("FUEL1", 5)}, player="a")
    assert wait_for(lambda: a.connected)  # a must seed the box before b joins
    b = client(relay.url, {}, player="b")
    try:
        assert wait_for(lambda: b.connected)
        rev = a.current_state()[0]
        outcome, rid = a.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take", rev)
        assert outcome == "ok"
        time.sleep(0.3)
        # the relay only expires a hold when the next message arrives
        b.request({}, "take", rev)
        a.commit(rid, {})
        got = []
        assert wait_for(lambda: got.extend(a.take_commit_rejected()) or got, timeout=2)
        assert got == [rid] and a.take_commit_rejected() == []
    finally:
        a.stop()
        b.stop()


def test_committed_is_reported_with_its_id(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)})
    try:
        assert wait_for(lambda: a.connected)
        outcome, rid = a.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take", a.current_state()[0])
        assert outcome == "ok"
        a.commit(rid, {"0,0": rec("FUEL1", 1)})
        got = []
        assert wait_for(lambda: got.extend(a.take_committed()) or got)
        assert got == [rid]
    finally:
        a.stop()


def test_deny_reason_is_kept(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)})
    try:
        assert wait_for(lambda: a.connected)
        assert a.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take", 99)[0] == "deny"
        assert a.last_deny_reason == "stale"
    finally:
        a.stop()


def test_resent_commit_from_new_connection_accepted(relay):
    # The connection drops after "ok"; the reconnected client resends the commit inside the hold.
    a = client(relay.url, {"0,0": rec("FUEL1", 5)})
    a.RECONNECT_SECONDS = 0.05
    try:
        assert wait_for(lambda: a.connected)
        epoch = a.connection_epoch
        outcome, rid = a.request({"0,0": {"id": "FUEL1", "amount": 5}}, "take", a.current_state()[0])
        assert outcome == "ok"
        a._conn.close()
        assert wait_for(lambda: a.connected and a.connection_epoch > epoch)
        a.commit(rid, {"0,0": rec("FUEL1", 2)})
        got = []
        assert wait_for(lambda: got.extend(a.take_committed()) or got)
        assert got == [rid]
        assert wait_for(lambda: a.current_state()[0] == 2)
        assert a.current_state()[1]["0,0"]["amount"] == 2
    finally:
        a.stop()


def test_refused_player_connects_soon_after_emptying_box(relay):
    a = client(relay.url, {"0,0": rec("FUEL1", 5)}, player="a")
    assert wait_for(lambda: a.connected)
    box = {"1,1": rec("LAND1", 2)}
    b = SyncClient(relay.url, "t", "b", lambda: (dict(box), None, 10, 6))
    b.RECONNECT_SECONDS = 0.05  # refused retries after RECONNECT_SECONDS * 5
    b.start()
    try:
        assert wait_for(lambda: b.status.startswith("refused"))
        box.clear()
        assert wait_for(lambda: b.connected, timeout=3)
        assert b.current_state()[1]["0,0"]["amount"] == 5
    finally:
        a.stop()
        b.stop()


def test_hello_sends_valid_slots(relay):
    c = SyncClient(relay.url, "t", "p", lambda: ({"0,0": rec("FUEL1", 5)}, None, 10, 6, ["0,0", "1,0"]))
    c.start()
    try:
        assert wait_for(lambda: c.connected)
        assert relay.relay.box.valid_slots == ["0,0", "1,0"]
    finally:
        c.stop()
