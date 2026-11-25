"""Background client for the shared box relay.

The game thread calls request/commit/adjust and polls take_state; a network thread owns the
connection, reconnects on its own, and hands replies back through events.
"""
import json
import logging
import threading
import time
import uuid

from websockets.sync.client import connect

logger = logging.getLogger("SharedBox")


class SyncClient:
    RECONNECT_SECONDS = 2.0
    KEEPALIVE_SECONDS = 20.0
    PONG_WAIT_SECONDS = 10.0

    def __init__(self, url, token, player, hello_source):
        self.url = url
        self.token = token
        self.player = player
        self.hello_source = hello_source
        self.status = "starting"
        self.last_rtt_ms = None
        self._ws = None
        self._conn = None  # raw socket, set as soon as connect() returns so stop() can close it
        self._lock = threading.Lock()
        self._new_state = None
        self._rejected_ids = []   # commits the relay refused (its hold had expired)
        self._committed_ids = []  # commits the relay confirmed
        self.last_deny_reason = None
        self.connection_epoch = 0  # increments each time a connection becomes "connected"
        self._current = None
        self._waiters = {}  # request id -> [Event, reply]
        self._stop = threading.Event()
        self._thread = None

    @property
    def connected(self):
        return self._ws is not None and self.status == "connected"

    def start(self):
        self._thread = threading.Thread(target=self._run, name="SharedBoxSync", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._conn
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self):
        while not self._stop.is_set():
            self.status = "connecting"
            try:
                with connect(self.url, open_timeout=5, close_timeout=1) as ws:
                    self._conn = ws
                    if self._stop.is_set():
                        break
                    source = self.hello_source()
                    slots, last_rev, width, height = source[:4]
                    hello = {"t": "hello", "token": self.token, "player": self.player,
                             "last_rev": last_rev, "slots": slots, "width": width, "height": height}
                    if len(source) > 4 and source[4] is not None:
                        hello["valid_slots"] = source[4]
                    ws.send(json.dumps(hello))
                    first = json.loads(ws.recv(timeout=10))
                    if first["t"] != "state":
                        self.status = f"{first['t']}: {first.get('reason', '?')}"
                        logger.error(f"relay said {first}")
                        # A refused player is told to empty the box; retry soon so that is picked up.
                        self._stop.wait(self.RECONNECT_SECONDS * 5 if first["t"] == "refused" else 30)
                        continue
                    self._set_state(first)
                    self._ws = ws
                    with self._lock:
                        self.connection_epoch += 1
                    self.status = "connected"
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=self.KEEPALIVE_SECONDS)
                        except TimeoutError:
                            if not ws.ping().wait(self.PONG_WAIT_SECONDS):
                                logger.info("relay did not answer ping, reconnecting")
                                break
                            continue
                        self._on_message(json.loads(raw))
            except Exception as e:
                logger.info(f"relay connection lost: {e}")
            finally:
                self._ws = None
                self._conn = None
                if self.status in ("connected", "connecting"):
                    self.status = "offline"
                self._fail_waiters()
            self._stop.wait(self.RECONNECT_SECONDS)

    def _set_state(self, msg):
        with self._lock:
            self._new_state = (msg["rev"], msg["slots"])
            self._current = self._new_state

    def _on_message(self, msg):
        kind = msg.get("t")
        if kind == "state":
            self._set_state(msg)
        elif kind in ("ok", "deny"):
            with self._lock:
                waiter = self._waiters.get(msg.get("id"))
            if waiter is not None:
                waiter[1] = msg
                waiter[0].set()
        elif kind == "commit_rejected":
            logger.error(f"relay rejected commit {msg.get('id')}; its hold had expired")
            with self._lock:
                self._rejected_ids.append(msg.get("id"))
        elif kind == "committed":
            with self._lock:
                self._committed_ids.append(msg.get("id"))

    def _fail_waiters(self):
        with self._lock:
            for waiter in self._waiters.values():
                waiter[0].set()

    def request(self, expect, kind, base_rev, timeout=1.5):
        ws = self._ws
        if ws is None or not self.connected:
            return "offline", None
        rid = uuid.uuid4().hex
        waiter = [threading.Event(), None]
        with self._lock:
            self._waiters[rid] = waiter
        start = time.monotonic()
        try:
            ws.send(json.dumps({"t": "request", "id": rid, "kind": kind, "base_rev": base_rev, "expect": expect}))
            if not waiter[0].wait(timeout):
                return "timeout", rid
        except Exception:
            return "offline", None
        finally:
            with self._lock:
                self._waiters.pop(rid, None)
        if waiter[1] is None:
            return "offline", None
        self.last_rtt_ms = (time.monotonic() - start) * 1000
        if waiter[1]["t"] == "ok":
            return "ok", rid
        self.last_deny_reason = waiter[1].get("reason")
        return "deny", rid

    def _send(self, msg):
        ws = self._ws
        if ws is None:
            logger.error(f"not connected, dropped {msg['t']}")
            return
        try:
            ws.send(json.dumps(msg))
        except Exception as e:
            logger.error(f"send {msg['t']} failed: {e}")

    def commit(self, rid, slots):
        self._send({"t": "commit", "id": rid, "slots": slots})

    def adjust(self, deltas, items):
        self._send({"t": "adjust", "deltas": deltas, "items": items})

    @property
    def commit_rejected_pending(self):
        with self._lock:
            return bool(self._rejected_ids)

    def take_commit_rejected(self):
        """Ids of the commits the relay rejected since the last call (empty list if none)."""
        with self._lock:
            rejected, self._rejected_ids = self._rejected_ids, []
        return rejected

    def take_committed(self):
        """Ids of the commits the relay confirmed since the last call."""
        with self._lock:
            done, self._committed_ids = self._committed_ids, []
        return done

    def take_state(self):
        with self._lock:
            state, self._new_state = self._new_state, None
        return state

    def current_state(self):
        with self._lock:
            return self._current
