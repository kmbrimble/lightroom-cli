"""Client host default must be the IPv4 literal (Windows resolves "localhost" to ::1 first; ~2 s per refused try)."""

import asyncio
import inspect
import time

import pytest

from lightroom_sdk.client import LightroomClient
from lightroom_sdk.resilient_bridge import ResilientSocketBridge
from lightroom_sdk.socket_bridge import SocketBridge


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("LR_BRIDGE_HOST", raising=False)


def test_constructors_default_to_ipv4_literal(tmp_path):
    pf = str(tmp_path / "p.txt")
    assert SocketBridge(port_file=pf).host == "127.0.0.1"
    assert ResilientSocketBridge(port_file=pf)._host == "127.0.0.1"
    assert LightroomClient(port_file=pf).host == "127.0.0.1"
    assert LightroomClient(port_file=pf)._bridge._host == "127.0.0.1"


def test_no_localhost_default_in_signatures():
    for cls in (SocketBridge, ResilientSocketBridge, LightroomClient):
        assert inspect.signature(cls.__init__).parameters["host"].default is None


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LR_BRIDGE_HOST", "10.0.0.5")
    assert SocketBridge(port_file=str(tmp_path / "p.txt")).host == "10.0.0.5"
    assert SocketBridge(host="1.2.3.4").host == "1.2.3.4"


async def test_connect_to_ipv4_only_listener_is_fast(tmp_path):
    """IPv4-only listeners (like the plugin). With host "localhost" and a ::1-first resolver this stalls."""

    async def hold(r, w):
        await asyncio.sleep(5)

    s1 = await asyncio.start_server(hold, "127.0.0.1", 0)
    s2 = await asyncio.start_server(hold, "127.0.0.1", 0)
    p1, p2 = (s.sockets[0].getsockname()[1] for s in (s1, s2))
    pf = tmp_path / "ports.txt"
    pf.write_text(f"{p1},{p2}")
    bridge = SocketBridge(port_file=str(pf))
    t = time.monotonic()
    try:
        await bridge.connect()
    finally:
        s1.close()
        s2.close()
    assert time.monotonic() - t < 1.0
