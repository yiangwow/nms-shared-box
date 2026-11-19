"""Check a relay is reachable and the token works, without touching the box.
Usage: python tools/relay_ping.py wss://nms.xyyoutube.com TOKEN"""
import json
import sys

from websockets.sync.client import connect

url, token = sys.argv[1], sys.argv[2]
with connect(url, open_timeout=10) as ws:
    ws.send(json.dumps({"t": "hello", "token": token, "player": "ping", "probe": True}))
    print(ws.recv(timeout=10))
