import asyncio
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

import pytest

from .conftest import Http, make_daemon

ROOT = Path(__file__).resolve().parents[2]
LRC = str(ROOT / "lightroom_sdk" / "lrc.py")


async def lrc(daemon_home, *args, stdin=None, env_extra=None):
    env = {"LRD_HOME": daemon_home, "PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    env.update(env_extra or {})
    t0 = time.perf_counter()
    p = await asyncio.create_subprocess_exec(sys.executable, "-S", "-E", LRC, *args, env=env,
                                             stdin=asyncio.subprocess.PIPE if stdin else None,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(stdin.encode() if stdin else None), 30)
    return p.returncode, (json.loads(out) if out.strip() else None), err.decode(), time.perf_counter() - t0


async def test_lrc_exit_codes(daemon, mock):
    h = daemon.home
    rc, o, _, _ = await lrc(h, "call", "system.ping")
    assert rc == 0 and o["ok"]
    rc, o, _, _ = await lrc(h, "ping")
    assert rc == 0
    mock.errors["catalog.getAllPhotos"] = "BOOM"
    rc, o, _, _ = await lrc(h, "call", "catalog.getAllPhotos")
    assert rc == 1 and o["error"]["code"] == "BOOM"
    mock.selection = [1]
    rc, o, _, _ = await lrc(h, "call", "develop.setValue", '{"parameter":"Exposure","value":1}')
    assert rc == 2 and o["error"]["code"] == "GUARD_DENIED"
    rc, o, _, _ = await lrc(h, "call", "catalog.removeFromCatalog", '{"photoId":1}')
    assert rc == 2
    rc, o, _, _ = await lrc(h, "call", "catalog.removeFromCatalog", '{"photoId":1}', "--allow-remove-from-catalog")
    assert rc == 0
    mock.drop.add("catalog.getKeywords")
    rc, o, _, _ = await lrc(h, "call", "catalog.getKeywords", "--timeout", "0.3")
    assert rc == 4 and o["error"]["code"] == "LR_TIMEOUT"
    rc, o, _, _ = await lrc(h, "health")
    assert rc == 0 and o["bridge"] == "connected"
    rc, o, _, _ = await lrc(h, "batch", "-", stdin=json.dumps([{"command": "system.ping"}, {"command": "catalog.setFlag", "params": {"photoId": 1}}]))
    assert rc == 0 and len(o["results"]) == 2
    rc, o, err, _ = await lrc(h, "call", "system.ping", "{not json")
    assert rc == 1 and o["error"]["code"] == "BAD_REQUEST"


async def test_lrc_bridge_down_is_exit_3(daemon, mock):
    await mock.stop()
    await asyncio.sleep(0.3)
    rc, o, _, _ = await lrc(daemon.home, "call", "system.ping", "--wait-bridge", "0")
    assert rc == 3 and o["error"]["code"] == "BRIDGE_DOWN"


async def test_lrc_daemon_not_running_is_exit_3_no_selfheal_on_linux(tmp_path):
    rc, o, _, dt = await lrc(str(tmp_path / "nohome"), "call", "system.ping")
    assert rc == 3 and o["error"]["code"] == "DAEMON_DOWN" and dt < 2


async def test_lrc_recovers_from_stale_token(daemon):
    sp = Path(daemon.home) / "lrd.json"
    good = json.loads(sp.read_text())
    sp.write_text(json.dumps({**good, "token": "stale"}))
    rc, o, _, _ = await lrc(daemon.home, "health")  # token re-read once, still 401 -> reported, not hung
    assert rc == 1 and o["error"]["code"] == "UNAUTHORIZED"
    sp.write_text(json.dumps(good))
    assert (await lrc(daemon.home, "health"))[0] == 0


async def test_lrc_latency_under_200ms(daemon):
    await lrc(daemon.home, "ping")  # warm file cache
    ts = [(await lrc(daemon.home, "ping"))[3] for _ in range(7)]
    med = statistics.median(ts)
    print(f"\nlrc ping wall: median {med*1000:.0f} ms, min {min(ts)*1000:.0f}, max {max(ts)*1000:.0f}")
    assert med < 0.2


def test_lrc_imports_only_stdlib_light_modules():
    src = (ROOT / "lightroom_sdk" / "lrc.py").read_text()
    import re

    mods = set(re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)", src, re.M))
    assert mods <= {"json", "os", "socket", "sys", "time", "subprocess", "__import__"}, mods


def test_daemon_runtime_has_no_third_party_imports():
    import re

    allowed = {"asyncio", "hashlib", "hmac", "json", "logging", "os", "re", "secrets", "subprocess", "sys",
               "tempfile", "time", "uuid", "dataclasses", "msvcrt", "fcntl", "signal"}
    for f in (ROOT / "lightroom_sdk" / "daemon").glob("*.py"):
        for m in re.findall(r"^\s*(?:import|from)\s+(\.?[a-zA-Z_][\w]*)", f.read_text(), re.M):
            assert m.startswith(".") or m in allowed, (f.name, m)


# ---- real process: python -m lightroom_sdk.daemon, singleton lock, stop (no leaked process)
async def test_daemon_process_singleton_and_stop(tmp_path, mock):
    import socket

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    home = str(tmp_path / "h")
    env = {**os.environ, "LRD_HOME": home, "LRD_PORT": str(port), "LR_PORT_FILE": str(tmp_path / "ports.txt"),
           "PYTHONPATH": str(ROOT)}
    p1 = await asyncio.create_subprocess_exec(sys.executable, "-m", "lightroom_sdk.daemon", env=env)
    try:
        for _ in range(100):
            if (Path(home) / "lrd.json").exists():
                break
            await asyncio.sleep(0.05)
        rc, o, _, _ = await lrc(home, "health")
        assert rc == 0 and o["pid"] == p1.pid
        for _ in range(100):  # bridge connects to the mock via LR_PORT_FILE
            rc, o, _, _ = await lrc(home, "health")
            if o["bridge"] == "connected":
                break
            await asyncio.sleep(0.1)
        assert o["bridge"] == "connected"
        assert (await lrc(home, "ping"))[0] == 0
        p2 = await asyncio.create_subprocess_exec(sys.executable, "-m", "lightroom_sdk.daemon", env=env)
        assert await asyncio.wait_for(p2.wait(), 10) == 0  # second instance exits quietly
        rc, o, _, _ = await lrc(home, "stop")
        assert rc == 0 and o["stopping"]
        assert await asyncio.wait_for(p1.wait(), 10) == 0
        assert not (Path(home) / "lrd.json").exists()
    finally:
        for p in (p1,):
            if p.returncode is None:
                p.kill()
                await p.wait()


# ---- soak (design 8.9): LRD_SOAK_N=10000 is the default
async def test_soak_random_disconnects_no_leaks(tmp_path, mock):
    n = int(os.environ.get("LRD_SOAK_N", "10000"))
    d = make_daemon(tmp_path)
    await d.start()
    assert await d.bridge.wait_up(5)
    h = Http(d)
    base = len(asyncio.all_tasks())
    rnd = random.Random(7)
    stop = False
    kills = 0

    async def chaos():
        nonlocal kills
        while not stop:
            await asyncio.sleep(rnd.uniform(0.2, 0.6))
            mock.kill_connection()
            kills += 1

    chaos_t = asyncio.create_task(chaos())
    sem = asyncio.Semaphore(32)
    codes = {}

    async def one(i):
        async with sem:
            r = await h.call("system.ping", wait_bridge=5)
            k = "ok" if r["ok"] else r["error"]["code"]
            codes[k] = codes.get(k, 0) + 1

    await asyncio.gather(*(one(i) for i in range(n)))
    stop = True
    await chaos_t
    assert await d.bridge.wait_up(8)
    print(f"\nsoak n={n} kills={kills} outcomes={codes}")
    assert set(codes) <= {"ok", "BRIDGE_LOST", "BRIDGE_DOWN"}
    assert codes.get("ok", 0) > n * 0.9 and kills >= 1
    await asyncio.sleep(0.2)
    assert d.bridge._pending == {} and d.sched.inflight == 0 and d.sched.queued == 0
    assert not d.sched.tasks and not d.front.conns
    assert not d.sched.m_lock.locked() and d.sched.r_sem._value == 4
    await d.stop(drain=1)
    await asyncio.sleep(0.1)
    assert len(asyncio.all_tasks()) <= base
