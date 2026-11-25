"""Act as a second player through the relay.
Take AMOUNT items from SLOT of the shared box:
  python tools/fake_player.py URL TOKEN SLOT AMOUNT          (SLOT like 0,0)
Put AMOUNT of item ID into the empty SLOT (copies the record of an existing stack of ID):
  python tools/fake_player.py URL TOKEN put SLOT ID AMOUNT"""
import json
import sys
import uuid

from websockets.sync.client import connect

url, token = sys.argv[1], sys.argv[2]
put = sys.argv[3] == "put"
if put:
    slot, item_id, amount = sys.argv[4], sys.argv[5], int(sys.argv[6])
else:
    slot, amount = sys.argv[3], int(sys.argv[4])


def recv_until(ws, kind, rid=None):
    while True:
        msg = json.loads(ws.recv(timeout=10))
        if msg["t"] == kind and (rid is None or msg.get("id") == rid):
            return msg
        if msg["t"] == "deny" and msg.get("id") == rid:
            sys.exit(f"denied: {msg['reason']}")


with connect(url, open_timeout=10) as ws:
    ws.send(json.dumps({"t": "hello", "token": token, "player": "fake", "last_rev": 0, "slots": {}}))
    first = recv_until(ws, "state")
    slots, rev = first["slots"], first["rev"]
    if put:
        if slot in slots:
            sys.exit(f"slot {slot} is not empty")
        src = next((s for s in slots.values() if s["id"] == item_id), None)
        if src is None:
            sys.exit(f"no existing stack of {item_id} to copy")
        if amount > src["max"]:
            sys.exit(f"{amount} exceeds max {src['max']} for {item_id}")
        rid = uuid.uuid4().hex
        ws.send(json.dumps({"t": "request", "id": rid, "kind": "put", "base_rev": rev, "expect": {slot: None}}))
        recv_until(ws, "ok", rid)
        ws.send(json.dumps({"t": "commit", "id": rid, "slots": dict(slots, **{slot: dict(src, amount=amount)})}))
        state = recv_until(ws, "state")
        print(f"put {amount} {item_id} into {slot}; relay rev {state['rev']}")
        sys.exit(0)
    if slot not in slots:
        sys.exit(f"slot {slot} is empty")
    stack = slots[slot]
    rid = uuid.uuid4().hex
    ws.send(json.dumps({"t": "request", "id": rid, "kind": "take", "base_rev": rev,
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
