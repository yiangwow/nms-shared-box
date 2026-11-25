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
        # Adjusts that arrive while a request is open. The holder's commit replaces the whole box, so
        # these are applied after it (or when the hold expires), never before where it would erase them.
        self.queued_adjusts = []
        self._timers = set()
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
            valid = hello.get("valid_slots")
            if decision == "seed":
                self.box.width = int(hello.get("width", self.box.width))
                self.box.height = int(hello.get("height", self.box.height))
                if isinstance(valid, list):
                    self.box.replace_valid_slots(valid)
                self.box.replace(local)
                save(self.box, self.path)
                logger.info("%s seeded the box with %d stacks", player, len(self.box.slots))
            elif isinstance(valid, list) and self.box.narrow_valid_slots(valid):
                # A relay seeded before valid_slots existed learns them from the next player to connect.
                save(self.box, self.path)
                logger.info("%s: new stacks limited to %d slots", player, len(self.box.valid_slots))
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

    def _apply_adjust(self, player, msg):
        result = self.box.adjust(msg.get("deltas") or {}, msg.get("items") or {})
        logger.info("%s adjust %s -> %s", player, msg.get("deltas"), result)

    def _release_hold(self):
        """Close the open request and apply the adjusts that waited for it. True if the box changed."""
        self.pending = None
        for timer in list(self._timers):  # only one hold exists at a time; its timer is done
            if timer is not asyncio.current_task():
                timer.cancel()
        queued, self.queued_adjusts = self.queued_adjusts, []
        for player, msg in queued:
            self._apply_adjust(player, msg)
        return bool(queued)

    async def _expire_later(self, rid, delay):
        await asyncio.sleep(delay)
        async with self.lock:
            if self.pending and self.pending[1] == rid:
                logger.warning("request %s expired without a commit", rid)
                if self._release_hold():
                    save(self.box, self.path)
                    await self._broadcast()

    async def _on_message(self, conn, player, msg):
        changed = False
        saved = False
        async with self.lock:
            if self.pending and self.pending[2] < time.monotonic():
                logger.warning("request %s expired without a commit", self.pending[1])
                changed = self._release_hold()
            kind = msg.get("t")
            if kind == "request":
                rid = msg.get("id")
                if self.pending:
                    await conn.send(json.dumps({"t": "deny", "id": rid, "reason": "busy"}))
                elif msg.get("base_rev") != self.box.rev:
                    await conn.send(json.dumps({"t": "deny", "id": rid, "reason": "stale"}))
                elif not self.box.matches(msg.get("expect") or {}):
                    await conn.send(json.dumps({"t": "deny", "id": rid, "reason": "stale"}))
                else:
                    self.pending = (conn, rid, time.monotonic() + PENDING_SECONDS)
                    timer = asyncio.create_task(self._expire_later(rid, PENDING_SECONDS))
                    self._timers.add(timer)
                    timer.add_done_callback(self._timers.discard)
                    await conn.send(json.dumps({"t": "ok", "id": rid}))
                    logger.info("%s %s approved (%s)", player, msg.get("kind"), rid)
            elif kind == "commit":
                rid = msg.get("id")
                # Matched by request id, not connection: a player who reconnects inside the hold resends it.
                if rid is not None and self.pending and self.pending[1] == rid:
                    self.box.replace(msg.get("slots") or {})
                    self.box.remember_commit(rid)
                    self._release_hold()
                    changed = True
                    # Persist before acking: if we die after the ack the mod would believe a move
                    # is recorded that box.json never saw.
                    save(self.box, self.path)
                    saved = True
                    await conn.send(json.dumps({"t": "committed", "id": rid, "rev": self.box.rev}))
                elif rid is not None and rid in self.box.recent_commits:
                    # A resend of a commit we already applied (its reply was lost): confirm, change nothing.
                    await conn.send(json.dumps({"t": "committed", "id": rid, "rev": self.box.rev}))
                    logger.info("%s resent commit %s, already applied", player, rid)
                else:
                    await conn.send(json.dumps({"t": "commit_rejected", "id": rid}))
                    logger.warning("%s sent a commit with no open request: %s", player, rid)
            elif kind == "adjust":
                if self.pending:
                    self.queued_adjusts.append((player, msg))
                    logger.info("%s adjust %s queued behind request %s", player, msg.get("deltas"), self.pending[1])
                else:
                    self._apply_adjust(player, msg)
                    changed = True
            if changed:
                if not saved:
                    save(self.box, self.path)
                await self._broadcast()


async def start(relay, host, port):
    return await serve(relay.handler, host, port)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("NMS_RELAY_TOKEN", "")
    if not token.strip():
        raise SystemExit("NMS_RELAY_TOKEN is empty; refusing to start a relay anyone could use")
    relay = Relay(token, os.environ.get("NMS_RELAY_DATA", "/data/box.json"))
    await start(relay, "0.0.0.0", int(os.environ.get("NMS_RELAY_PORT", "8080")))
    logger.info("nms-relay listening, box rev %d, %d stacks", relay.box.rev, len(relay.box.slots))
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
