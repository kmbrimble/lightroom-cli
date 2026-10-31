"""Minimal HTTP/1.1 front (one request per connection). Localhost-only; checks, in order:
Host == 127.0.0.1:<port> (403), method (405, no CORS headers ever), X-Lrd-Token (401), JSON content type (415)."""

import asyncio
import hmac
import json
import time

from .classes import CLASSES
from .scheduler import err

MAX_HEADER = 64 * 1024
MAX_BODY = 16 * 1024 * 1024
MAX_BATCH = 200
REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 413: "Payload Too Large", 415: "Unsupported Media Type"}


class Front:
    def __init__(self, daemon):
        self.d = daemon
        self.server = None
        self.conns = set()

    async def start(self, port):
        self.server = await asyncio.start_server(self._conn, "127.0.0.1", port, limit=MAX_HEADER)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        for t in list(self.conns):
            t.cancel()
        await asyncio.gather(*self.conns, return_exceptions=True)
        await self.server.wait_closed()

    async def _conn(self, reader, writer):
        task = asyncio.current_task()
        self.conns.add(task)
        try:
            status, obj = await self._handle(reader)
        except asyncio.CancelledError:
            writer.close()
            raise
        except Exception as e:  # malformed request etc.
            status, obj = 400, err("BAD_REQUEST", str(e))
        finally:
            self.conns.discard(task)
        try:
            body = json.dumps(obj).encode()
            head = (f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n").encode()
            writer.write(head + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def _handle(self, reader):
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        lines = raw.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        h = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                h[k.strip().lower()] = v.strip()
        if h.get("host") != f"127.0.0.1:{self.d.port}":
            return 403, err("FORBIDDEN", "bad Host header")
        if method not in ("GET", "POST"):
            return 405, err("METHOD_NOT_ALLOWED", method)
        if not hmac.compare_digest(h.get("x-lrd-token", "").encode(), self.d.token.encode()):
            return 401, err("UNAUTHORIZED", "missing or wrong X-Lrd-Token")
        body = {}
        if method == "POST":
            if h.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return 415, err("UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")
            n = int(h.get("content-length", "0"))
            if n > MAX_BODY:
                return 413, err("TOO_LARGE", "body too large")
            data = await asyncio.wait_for(reader.readexactly(n), 30) if n else b"{}"
            body = json.loads(data)
            if not isinstance(body, dict):
                return 400, err("BAD_REQUEST", "body must be a JSON object")
        return await self._route(method, path, body)

    async def _route(self, method, path, body):
        d = self.d
        if (method, path) == ("GET", "/v1/health"):
            return 200, d.health()
        if (method, path) == ("GET", "/v1/commands"):
            return 200, {"ok": True, "commands": CLASSES}
        if (method, path) == ("POST", "/v1/call"):
            return 200, await d.sched.call(body)
        if (method, path) == ("POST", "/v1/batch"):
            calls = body.get("calls")
            if not isinstance(calls, list) or len(calls) > MAX_BATCH or not all(isinstance(c, dict) for c in calls):
                return 400, err("BAD_REQUEST", f"calls must be a list of <= {MAX_BATCH} objects")
            if body.get("ordered", True):
                out = []
                for c in calls:
                    r = await d.sched.call(c)
                    out.append(r)
                    if not r.get("ok") and body.get("stop_on_error", True):
                        break
            else:
                out = await asyncio.gather(*(d.sched.call(c) for c in calls))
            return 200, {"ok": all(r.get("ok") for r in out) and len(out) == len(calls), "results": list(out)}
        if (method, path) == ("POST", "/v1/cancel"):
            found = await d.sched.cancel(str(body.get("requestId", "")))
            return 200, {"ok": True, "cancelled": found}
        if (method, path) == ("POST", "/v1/shutdown"):
            d.request_stop()
            return 200, {"ok": True, "stopping": True}
        return 404, err("NOT_FOUND", f"{method} {path}")
