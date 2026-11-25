import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "mod"))
sys.path.insert(0, os.path.join(ROOT, "relay"))


import asyncio
import threading

import pytest


class RelayThread:
    """Runs a real relay on a background event loop for client tests."""

    def __init__(self, path, token="t", port=0):
        import server as relay_server

        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)

            async def boot():
                self.relay = relay_server.Relay(token, path)
                self.srv = await relay_server.start(self.relay, "127.0.0.1", port)
                self.url = f"ws://127.0.0.1:{list(self.srv.sockets)[0].getsockname()[1]}"
                ready.set()

            self.loop.run_until_complete(boot())
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        assert ready.wait(5)

    def stop(self):
        async def down():
            for timer in list(self.relay._timers):
                timer.cancel()
            self.srv.close()
            await self.srv.wait_closed()

        asyncio.run_coroutine_threadsafe(down(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def relay(tmp_path):
    r = RelayThread(str(tmp_path / "box.json"))
    yield r
    r.stop()
