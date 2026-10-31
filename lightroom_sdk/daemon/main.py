"""lrd entry point: python -m lightroom_sdk.daemon  (stdlib only; logs to lrd.log, never prints: pythonw has no stdio)."""

import asyncio
import json
import logging
import logging.handlers
import os
import secrets
import subprocess
import sys
import tempfile
import time

from .audit import AuditLog
from .bridge import Bridge
from .front import Front
from .scheduler import Scheduler

DEFAULT_PORT = 54330
DEFAULT_BRIDGE = (54322, 54323)  # (plugin->client, client->plugin), per plugin Config.lua
log = logging.getLogger("lrd")


def home_dir():
    h = os.environ.get("LRD_HOME")
    if h:
        return h
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "lightroom-cli")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "lightroom-cli")


def port_file():
    return os.environ.get("LR_PORT_FILE") or os.path.join(tempfile.gettempdir(), "lightroom_ports.txt")


def ports_from_file(path, fallback=DEFAULT_BRIDGE):
    """Candidates: the port file (if parseable) first, then the plugin defaults. A stale file is harmless."""
    out = []
    try:
        with open(path) as f:
            a, b = f.read().strip().split(",")[:2]
        out.append((int(a), int(b)))
    except (OSError, ValueError):
        pass
    if fallback and fallback not in out:
        out.append(fallback)
    return out


class Daemon:
    def __init__(self, home, http_port=DEFAULT_PORT, ports_fn=None, backoff=None, grace=5.0, handshake_timeout=5.0):
        self.home = home
        self.http_port_cfg = http_port
        self.port = None
        self.token = secrets.token_urlsafe(24)
        self.started = time.time()
        kw = {"backoff": backoff} if backoff else {}
        self.bridge = Bridge(ports_fn or (lambda: ports_from_file(port_file())), handshake_timeout=handshake_timeout, **kw)
        self.audit = AuditLog(os.path.join(home, "audit.jsonl"))
        self.sched = Scheduler(self.bridge, self.audit, grace=grace)
        self.front = Front(self)
        self._stop = asyncio.Event()

    async def start(self):
        os.makedirs(self.home, exist_ok=True)
        self.port = await self.front.start(self.http_port_cfg)
        self.bridge.start()
        self._write_state()
        return self.port

    def _write_state(self):
        path = os.path.join(self.home, "lrd.json")
        tmp = path + ".tmp"
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started))
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        with os.fdopen(os.open(tmp, flags, 0o600), "w") as f:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid(), "started": started}, f)
        if sys.platform == "win32":  # restrict to the current user; best effort
            try:
                user = os.environ.get("USERNAME", "")
                subprocess.run(["icacls", tmp, "/inheritance:r", "/grant:r", f"{user}:F"], timeout=10,
                               capture_output=True, creationflags=0x08000000)
            except Exception:
                log.warning("icacls failed; lrd.json keeps inherited ACL")
        os.replace(tmp, path)

    def _remove_state(self):
        path = os.path.join(self.home, "lrd.json")
        try:
            with open(path) as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(path)
        except (OSError, ValueError):
            pass

    def health(self):
        b = self.bridge
        st = "connecting" if b.state == "handshake" else b.state
        return {"ok": True, "daemon": "up", "bridge": "connected" if st == "up" else st, "bridge_reason": b.reason,
                "inflight": self.sched.inflight, "queued": self.sched.queued,
                "uptime_s": int(time.time() - self.started), "pid": os.getpid(), "connects": b.connects,
                "last_event": b.last_event, "plugin": b.plugin_info}

    def request_stop(self):
        self._stop.set()

    async def serve(self):
        await self._stop.wait()
        await self.stop()

    async def stop(self, drain=5.0):
        self._stop.set()
        await asyncio.sleep(0.05)  # let the /v1/shutdown reply flush
        if self.sched.tasks:
            await asyncio.wait(set(self.sched.tasks), timeout=drain)
        await self.front.stop()
        await self.bridge.stop()
        for t in list(self.sched.tasks):
            t.cancel()
        await asyncio.gather(*self.sched.tasks, return_exceptions=True)
        self._remove_state()


def acquire_lock(home):
    """Singleton lock held for the life of the process. Returns the open file, or None if another lrd has it."""
    os.makedirs(home, exist_ok=True)
    f = open(os.path.join(home, "lrd.lock"), "a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def setup_logging(home):
    os.makedirs(home, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(os.path.join(home, "lrd.log"), maxBytes=2_000_000, backupCount=2,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(os.environ.get("LRD_LOG_LEVEL", "INFO"))


async def _run(home):
    d = Daemon(home, http_port=int(os.environ.get("LRD_PORT", DEFAULT_PORT)))
    try:
        await d.start()
    except OSError as e:
        log.error("cannot bind 127.0.0.1:%s: %s", d.http_port_cfg, e)
        return 1
    log.info("lrd up on 127.0.0.1:%s pid %s", d.port, os.getpid())
    for sig in ("SIGTERM", "SIGINT"):
        try:
            import signal

            asyncio.get_running_loop().add_signal_handler(getattr(signal, sig), d.request_stop)
        except (NotImplementedError, AttributeError, RuntimeError):
            pass  # Windows Proactor: shutdown is via POST /v1/shutdown
    await d.serve()
    log.info("lrd stopped")
    return 0


def main(argv=None):
    home = home_dir()
    setup_logging(home)
    lock = acquire_lock(home)
    if lock is None:
        log.info("another lrd holds the lock; exiting")
        return 0
    try:
        return asyncio.run(_run(home))
    except Exception:
        log.exception("lrd crashed")
        return 1
    finally:
        lock.close()
