"""BridgeConn: the ONLY code that talks to the Lightroom plugin (54323 send / 54322 receive, NDJSON).

State machine DOWN -> CONNECTING -> UP. Reconnects forever with backoff in a background task; requests
never trigger connect attempts (a refused loopback connect costs ~2 s on Windows). One connection pair.
"""

import asyncio
import json
import logging
import uuid

log = logging.getLogger("lrd.bridge")
LIMIT = 64 * 1024 * 1024  # base64 previews exceed asyncio's 64 KiB default line limit
BACKOFF = (0.5, 1, 2, 4, 8, 10)


class BridgeError(Exception):
    code = "BRIDGE_DOWN"


class BridgeDown(BridgeError):
    code = "BRIDGE_DOWN"


class BridgeLost(BridgeError):
    code = "BRIDGE_LOST"


class Bridge:
    def __init__(self, ports_fn, host="127.0.0.1", backoff=BACKOFF, connect_timeout=5.0, handshake_timeout=5.0):
        """ports_fn() -> list of (send_port, receive_port) candidates, tried in order each attempt.
        send_port = plugin->client (we READ); receive_port = client->plugin (we WRITE)."""
        self.ports_fn = ports_fn
        self.host = host
        self.backoff = backoff
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.state = "down"
        self.reason = "not started"
        self.plugin_info = {}
        self.last_event = None
        self.connects = 0
        self._pending = {}
        self._writer = None
        self._reader_w = None
        self._send_lock = asyncio.Lock()
        self._up = asyncio.Event()
        self._drop = None
        self._task = None
        self._stopping = False

    # ---- lifecycle
    def start(self):
        self._task = asyncio.create_task(self._run(), name="lrd-bridge")

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._close()
        self._fail_all(BridgeDown("daemon stopping"))

    async def wait_up(self, timeout):
        if self.state == "up":
            return True
        if not timeout:
            return False
        try:
            await asyncio.wait_for(self._up.wait(), timeout)
            return self.state == "up"
        except asyncio.TimeoutError:
            return False

    # ---- request path
    async def submit(self, command, params):
        """Send one command; return (plugin_request_id, future resolving to the reply dict)."""
        if self.state not in ("up", "handshake") or self._writer is None:
            raise BridgeDown(self.reason)
        rid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = (json.dumps({"id": rid, "command": command, "params": params or {}}) + "\n").encode()
        try:
            async with self._send_lock:
                self._writer.write(line)
                await self._writer.drain()
        except Exception as e:  # connection broke under us
            self._pending.pop(rid, None)
            self._signal_drop(f"send failed: {e}")
            raise BridgeLost(f"send failed: {e}") from e
        return rid, fut

    def forget(self, rid):
        self._pending.pop(rid, None)

    async def request(self, command, params, timeout):
        """Convenience: submit and await with a deadline (used for guard lookups and cancel)."""
        rid, fut = await self.submit(command, params)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            await self.cancel(rid)
            raise
        finally:
            self.forget(rid)

    async def cancel(self, rid):
        """Fire-and-forget cancelCommand; the plugin only sets a flag."""
        try:
            crid, fut = await self.submit("cancelCommand", {"requestId": rid})
            asyncio.get_running_loop().call_later(2.0, self.forget, crid)
        except BridgeError:
            pass

    # ---- internals
    def _signal_drop(self, why):
        if self._drop is not None and not self._drop.done():
            self._drop.set_result(why)

    def _close(self):
        for w in (self._writer, self._reader_w):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
        self._writer = self._reader_w = None

    def _fail_all(self, exc):
        pend, self._pending = self._pending, {}
        for f in pend.values():
            if not f.done():
                f.set_exception(type(exc)(str(exc)))
                f.exception()  # mark retrieved: abandoned waiters must not log "never retrieved"

    async def _connect_once(self):
        last = None
        for send_port, recv_port in self.ports_fn():
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection(self.host, recv_port, limit=LIMIT), self.connect_timeout)
                try:
                    r, rw = await asyncio.wait_for(
                        asyncio.open_connection(self.host, send_port, limit=LIMIT), self.connect_timeout)
                except BaseException:
                    w.close()
                    raise
                return r, w, rw
            except (OSError, asyncio.TimeoutError) as e:
                last = e
        raise OSError(f"cannot connect to plugin: {last}")

    async def _run(self):
        attempt = 0
        while not self._stopping:
            self.state, self._drop = "connecting", asyncio.get_running_loop().create_future()
            self._up.clear()
            try:
                reader, self._writer, self._reader_w = await self._connect_once()
            except Exception as e:
                self.state, self.reason = "down", f"plugin not listening ({e}); start it via File > Plug-in Extras > Start CLI Bridge"
                await asyncio.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
                attempt += 1
                continue
            rtask = asyncio.create_task(self._read_loop(reader))
            try:
                # handshake: a backlog-only (unserviced) connection never answers
                self.state = "handshake"
                _, fut = await self.submit("system.ping", {})
                done, _p = await asyncio.wait({fut, self._drop}, timeout=self.handshake_timeout,
                                               return_when=asyncio.FIRST_COMPLETED)
                if fut not in done:
                    raise BridgeLost(self._drop.result() if self._drop in done else "no reply")
                rep = fut.result()
                self.plugin_info = (rep.get("result") or {}) if isinstance(rep, dict) else {}
            except Exception as e:
                self.state, self.reason = "down", f"plugin did not answer handshake (another client attached?): {e!r}"
                rtask.cancel()
                self._close()
                self._fail_all(BridgeLost(self.reason))
                await asyncio.gather(rtask, return_exceptions=True)
                await asyncio.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
                attempt += 1
                continue
            attempt = 0
            self.connects += 1
            self.state, self.reason = "up", "connected"
            self._up.set()
            log.info("bridge up (connect #%d)", self.connects)
            why = await self._drop
            self.state, self.reason = "down", f"connection lost: {why}"
            self._up.clear()
            rtask.cancel()
            self._close()
            self._fail_all(BridgeLost(self.reason))
            await asyncio.gather(rtask, return_exceptions=True)
            log.warning("bridge down: %s", why)
            await asyncio.sleep(self.backoff[0])

    async def _read_loop(self, reader):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    self._signal_drop("EOF")
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("invalid JSON from plugin (%d bytes)", len(line))
                    continue
                if not isinstance(msg, dict):
                    continue
                if "event" in msg:
                    self.last_event = msg.get("event")
                    if msg["event"] == "server.shutdown":
                        self._signal_drop("server.shutdown")
                    continue
                f = self._pending.pop(msg.get("id"), None)
                if f is not None and not f.done():
                    f.set_result(msg)
                # else: late reply after deadline/forget, or streaming chunk: dropped by design
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._signal_drop(f"read error: {e!r}")
