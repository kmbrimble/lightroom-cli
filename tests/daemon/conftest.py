import asyncio
import json
import os
import sys

import pytest
import pytest_asyncio

from lightroom_sdk.daemon.main import Daemon, ports_from_file

from .mock_plugin import MockPlugin

FAST = (0.05, 0.1, 0.2)


@pytest_asyncio.fixture
async def mock(tmp_path):
    m = MockPlugin(port_file=tmp_path / "ports.txt", restart_delay=0.2)
    await m.start()
    yield m
    await m.stop()


def make_daemon(tmp_path, mock=None, **kw):
    pf = str(tmp_path / "ports.txt")
    kw.setdefault("backoff", FAST)
    kw.setdefault("grace", 0.3)
    kw.setdefault("handshake_timeout", 1.0)
    return Daemon(str(tmp_path / "home"), http_port=0, ports_fn=lambda: ports_from_file(pf, fallback=None), **kw)


@pytest_asyncio.fixture
async def daemon(tmp_path, mock):
    d = make_daemon(tmp_path, mock)
    await d.start()
    assert await d.bridge.wait_up(5), d.bridge.reason
    yield d
    await d.stop(drain=1)


class Http:
    """Tiny async HTTP client for tests (raw so we can send bad Host/Content-Type)."""

    def __init__(self, d):
        self.d = d

    async def raw(self, method, path, body=None, headers=None, host=None, token=True, ctype="application/json"):
        h = {"Host": host or f"127.0.0.1:{self.d.port}", "Connection": "close"}
        if token:
            h["X-Lrd-Token"] = self.d.token if token is True else token
        if ctype:
            h["Content-Type"] = ctype
        data = json.dumps(body).encode() if body is not None else b""
        h["Content-Length"] = str(len(data))
        h.update(headers or {})
        r, w = await asyncio.open_connection("127.0.0.1", self.d.port)
        w.write((f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + data)
        await w.drain()
        buf = await r.read()
        w.close()
        head, _, b = buf.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        hdrs = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:]}
        return int(lines[0].split()[1]), hdrs, (json.loads(b) if b else None)

    async def call(self, command, params=None, **kw):
        body = {"command": command, "params": params or {}, **kw}
        st, _, obj = await self.raw("POST", "/v1/call", body)
        assert st == 200, (st, obj)
        return obj


@pytest.fixture
def http(daemon):
    return Http(daemon)
