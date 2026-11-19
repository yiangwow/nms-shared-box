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
