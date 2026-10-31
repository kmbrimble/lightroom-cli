"""Faithful mock of the Lightroom CLI Bridge plugin's socket behaviour (design section 8.1).

* Two listeners (send: plugin->client, receive: client->plugin), IPv4 loopback.
* Each listener services exactly ONE connection: accept() is called once per cycle, so further clients
  sit in the kernel backlog and are never read (the real LrSocket behaviour).
* Closing either connection restarts both listeners after `restart_delay` (refused in between) and rewrites
  the port file.
* Every command is handled in its own task; replies are matched by id and may arrive out of order.
* Records concurrency per class so tests can assert lane invariants independently of the daemon.
"""
import asyncio
import json
import os
import socket

from lightroom_sdk.daemon.classes import CLASSES


def _listen(port=0):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(16)
    s.setblocking(False)
    return s


class MockPlugin:
    def __init__(self, port_file=None, restart_delay=0.3, latency=0.0, **kw):
        self.port_file = str(port_file) if port_file else None
        self.restart_delay = restart_delay
        self.latency = latency            # float or {command: seconds}
        self.drop = set()                 # commands that never get a reply
        self.hang = set()                 # commands that hang until released/cancelled (then reply)
        self.errors = {}                  # command -> error code
        self.photos = {1: {"isVirtualCopy": False}, 2: {"isVirtualCopy": True}, 3: {"isVirtualCopy": True}}
        self.selection = [1]
        self.next_id = 100
        self.received = []                # (command, params)
        self.writes = []                  # non-read commands that executed
        self.cancelled = []               # requestIds from cancelCommand
        self.active = []                  # currently executing commands
        self.max_r = 0
        self.violations = []
        self.connects = 0
        self.restarts = 0
        self.ports = None                 # (send_port, receive_port)
        self._run = False
        self._conns = []
        self._tasks = set()
        self._release = {}
        self._main = None
        self._send_w = None
        self._lock = None
        self._cycle_done = None

    # -- lifecycle
    async def start(self):
        self._run = True
        self._lock = asyncio.Lock()
        self._main = asyncio.create_task(self._serve())
        while self.ports is None:
            await asyncio.sleep(0.005)
        return self.ports

    async def stop(self):
        """Lightroom gone: listeners and connections vanish for good."""
        self._run = False
        if self._main:
            self._main.cancel()
            await asyncio.gather(self._main, return_exceptions=True)
        self._drop_conns()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._main = None
        self._release_all()

    def _release_all(self):
        for e in self._release.values():
            e.set()

    def _drop_conns(self):
        for w in self._conns:
            try:
                w.transport.abort()
            except Exception:
                pass
        self._conns = []
        self._send_w = None

    def kill_connection(self):
        """Abruptly drop the attached client (plugin then restarts its listeners)."""
        self._drop_conns()

    async def send_event(self, name, data=None):
        if self._send_w:
            self._send_w.write((json.dumps({"event": name, "data": data or {}}) + "\n").encode())
            await self._send_w.drain()

    async def shutdown_event(self):
        await self.send_event("server.shutdown", {"reason": "Lightroom closing"})

    def release(self, command):
        self._release.setdefault(command, asyncio.Event()).set()

    # -- socket cycle
    async def _serve(self):
        loop = asyncio.get_running_loop()
        fixed = (0, 0)
        while self._run:
            ls_send, ls_recv = _listen(fixed[0]), _listen(fixed[1])
            fixed = (ls_send.getsockname()[1], ls_recv.getsockname()[1])
            self.ports = fixed
            if self.port_file:
                with open(self.port_file, "w") as f:
                    f.write(f"{fixed[0]},{fixed[1]}")
            try:
                # each listener services exactly one connection; extra clients stay in the backlog
                (c_recv, _), (c_send, _) = await asyncio.gather(
                    loop.sock_accept(ls_recv), loop.sock_accept(ls_send))
            except asyncio.CancelledError:
                ls_send.close()
                ls_recv.close()
                raise
            self.connects += 1
            r_in, w_in = await asyncio.open_connection(sock=c_recv, limit=1 << 26)
            r_out, w_out = await asyncio.open_connection(sock=c_send)
            self._conns = [w_in, w_out]
            self._send_w = w_out
            closed = asyncio.get_running_loop().create_future()
            tasks = [asyncio.create_task(self._reader(r_in, closed)),
                     asyncio.create_task(self._eof_watch(r_out, closed))]
            try:
                await closed
            except asyncio.CancelledError:
                for t in tasks:
                    t.cancel()
                ls_send.close()
                ls_recv.close()
                raise
            for t in tasks:
                t.cancel()
            self._drop_conns()
            ls_send.close()  # listeners down during restart: connects are refused
            ls_recv.close()
            self.restarts += 1
            await asyncio.sleep(self.restart_delay)

    async def _eof_watch(self, reader, closed):
        try:
            while await reader.read(4096):
                pass
        except Exception:
            pass
        if not closed.done():
            closed.set_result("send side closed")

    async def _reader(self, reader, closed):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line)
                t = asyncio.create_task(self._handle(msg))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
        except Exception:
            pass
        if not closed.done():
            closed.set_result("receive side closed")

    # -- command handling
    async def _reply(self, mid, ok, result=None, error=None):
        w = self._send_w
        if w is None:
            return
        m = {"id": mid, "success": ok, "timestamp": 0}
        if ok:
            m["result"] = result
        else:
            m["error"] = error
        try:
            w.write((json.dumps(m) + "\n").encode())
            await w.drain()
        except Exception:
            pass

    async def _handle(self, msg):
        cmd, params, mid = msg.get("command"), msg.get("params") or {}, msg.get("id")
        if cmd == "cancelCommand":
            self.cancelled.append(params.get("requestId"))
            ev = self._release.setdefault(params.get("requestId"), asyncio.Event())
            ev.set()
            return await self._reply(mid, True, {"cancelled": True, "requestId": params.get("requestId")})
        self.received.append((cmd, params))
        klass = CLASSES.get(cmd)
        is_read = klass == "R"
        # lane invariants as seen by the "plugin"
        if is_read:
            if any(CLASSES.get(a) != "R" for a in self.active):
                self.violations.append(("read while non-read active", cmd, list(self.active)))
        elif self.active:
            self.violations.append(("non-read while busy", cmd, list(self.active)))
        self.active.append(cmd)
        self.max_r = max(self.max_r, sum(1 for a in self.active if CLASSES.get(a) == "R"))
        try:
            lat = self.latency.get(cmd, self.latency.get("*", 0)) if isinstance(self.latency, dict) else self.latency
            if lat:
                await asyncio.sleep(lat)
            if cmd in self.drop:
                return
            if cmd in self.hang:
                ev = self._release.setdefault(mid, asyncio.Event())
                cev = self._release.setdefault(cmd, asyncio.Event())
                await asyncio.wait({asyncio.ensure_future(ev.wait()), asyncio.ensure_future(cev.wait())},
                                   return_when=asyncio.FIRST_COMPLETED)
            if cmd in self.errors:
                return await self._reply(mid, False, error={"code": self.errors[cmd], "message": "mock error",
                                                            "severity": "error", "details": {}})
            if not is_read and cmd not in ("catalog.setSelectedPhotos",):
                self.writes.append((cmd, params))
            await self._reply(mid, True, self._result(cmd, params))
        finally:
            self.active.remove(cmd)

    def _result(self, cmd, params):
        if cmd == "system.ping":
            return {"pong": True, "version": "mock"}
        if cmd == "catalog.getSelectedPhotos":
            ps = [self._photo(i) for i in self.selection]
            return {"photos": ps, "count": len(ps)}
        if cmd == "catalog.getPhotoMetadata":
            return self._photo(int(params.get("photoId", 0)))
        if cmd == "catalog.setSelectedPhotos":
            self.selection = [int(i) for i in params.get("photoIds", [])]
            return {"count": len(self.selection)}
        if cmd == "catalog.createVirtualCopy":
            self.next_id += 1
            self.photos[self.next_id] = {"isVirtualCopy": True}
            self.selection = [self.next_id]
            return {"photoId": self.next_id}
        return {"echo": cmd}

    def _photo(self, pid):
        p = self.photos.get(pid)
        d = {"id": pid}
        if p is not None:
            d.update(p)
        return d
