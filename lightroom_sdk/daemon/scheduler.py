"""Scheduler + Guard host.

Lane R: class R only, Semaphore(4). Lane M: everything else, one asyncio.Lock (FIFO) that also takes all
R slots, so writes never overlap reads. The Lane-M lock is held across guard resolve + check + execute.
Deadlines: reply LR_TIMEOUT, send cancelCommand, and (lane M) keep the lock until the plugin answers or
GRACE passes so a still-running write cannot overlap the next command.
"""

import asyncio
import time

from . import guard
from .audit import AuditLog
from .bridge import BridgeDown, BridgeError

R_SLOTS = 4
GRACE = 5.0
DEFAULT_TIMEOUT = 30.0
LOOKUP_TIMEOUT = 10.0
# command prefix/name -> default deadline (seconds), mirrors the SDK's long-command table
LONG = {"preview.": 110.0, "develop.batchAIMask": 280.0, "develop.batchApplySettings": 120.0}


def err(code, message, **details):
    return {"ok": False, "error": {"code": code, "message": message, "details": details}}


def default_timeout(command):
    for k, v in LONG.items():
        if command.startswith(k):
            return v
    return DEFAULT_TIMEOUT


class Scheduler:
    def __init__(self, bridge, audit: AuditLog, grace=GRACE):
        self.bridge = bridge
        self.audit = audit
        self.grace = grace
        self.r_sem = asyncio.Semaphore(R_SLOTS)
        self.m_lock = asyncio.Lock()
        self.active = {}  # client request_id -> plugin id
        self.inflight = 0
        self.queued = 0
        self.tasks = set()

    def spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def call(self, body):
        """Run one /v1/call body to completion. Shielded from client disconnects."""
        return await asyncio.shield(self.spawn(self._call(body)))

    async def cancel(self, request_id):
        rid = self.active.get(request_id)
        if rid:
            await self.bridge.cancel(rid)
        return rid is not None

    async def _call(self, body):
        t0 = time.monotonic()
        command = body.get("command")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        client = str(body.get("client") or "?")[:64]
        flags = body.get("flags")
        creq = body.get("request_id")
        v = guard.precheck(command, params, flags)
        affected, outcome, decision = [], "", "allow"
        reply = None
        read = v.allow and v.klass in guard.READ_CLASSES
        try:
            if not v.allow:
                decision, reply = "deny", {"ok": False, "error": v.error()}
                outcome = "GUARD_DENIED"
                return reply
            try:
                timeout = float(body.get("timeout") or default_timeout(command))
                wait_bridge = float(body.get("wait_bridge", 2))
            except (TypeError, ValueError):
                reply = err("BAD_REQUEST", "timeout/wait_bridge must be numbers")
                return reply
            up_by = t0 + wait_bridge
            if not await self.bridge.wait_up(wait_bridge):
                reply = err("BRIDGE_DOWN", self.bridge.reason)
                return reply
            self.queued += 1
            waiting = True
            try:
                if read:
                    async with self.r_sem:
                        self.queued -= 1
                        waiting = False
                        q_ms = int((time.monotonic() - t0) * 1000)
                        reply = await self._exec(command, params, timeout, creq, q_ms, exclusive=False, up_by=up_by)
                else:
                    async with self.m_lock:
                        slots = 0
                        try:
                            for _ in range(R_SLOTS):  # drain reads, block new ones
                                await self.r_sem.acquire()
                                slots += 1
                            self.queued -= 1
                            waiting = False
                            q_ms = int((time.monotonic() - t0) * 1000)
                            if v.needs_resolve:
                                ok, affected, details = await self._resolve(params)
                                if not ok:
                                    decision, outcome = "deny", "GUARD_DENIED"
                                    v.reason = "Develop writes are only allowed on virtual copies"
                                    v.hint, v.details = guard.HINT_VC, details
                                    reply = {"ok": False, "error": v.error()}
                                    return reply
                            reply = await self._exec(command, params, timeout, creq, q_ms, exclusive=True, up_by=up_by)
                        finally:
                            for _ in range(slots):
                                self.r_sem.release()
            except BridgeError as e:
                reply = err(e.code, str(e), outcome="unknown" if not read else "none")
            finally:
                if waiting:
                    self.queued -= 1
            return reply
        except BaseException as e:
            if reply is None:
                reply = err("DAEMON_ERROR", repr(e))
            raise
        finally:
            if not outcome:
                outcome = "ok" if reply and reply.get("ok") else (reply or {}).get("error", {}).get("code", "error")
            if not read:
                self.audit.write(client, command, params, affected, decision, outcome)

    async def _resolve(self, params):
        async def lookup(c, p):
            try:
                return await self.bridge.request(c, p, LOOKUP_TIMEOUT)
            except asyncio.TimeoutError:
                return {"success": False, "error": {"code": "LR_TIMEOUT", "message": "lookup timed out"}}

        return await guard.resolve_and_check(params, lookup)

    async def _submit(self, command, params, up_by):
        """submit(); if the link dropped between wait_up and now nothing was sent, so waiting and retrying is safe."""
        while True:
            try:
                return await self.bridge.submit(command, params)
            except BridgeDown:
                left = up_by - time.monotonic()
                if left <= 0 or not await self.bridge.wait_up(left):
                    raise

    async def _exec(self, command, params, timeout, creq, q_ms, exclusive, up_by):
        t1 = time.monotonic()
        rid, fut = await self._submit(command, params, up_by)
        if creq:
            self.active[creq] = rid
        self.inflight += 1
        try:
            try:
                rep = await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                await self.bridge.cancel(rid)
                if exclusive:  # keep the lane until the plugin answers or grace passes
                    try:
                        await asyncio.wait_for(asyncio.shield(fut), self.grace)
                    except (asyncio.TimeoutError, BridgeError):
                        pass
                return err("LR_TIMEOUT", f"{command} exceeded {timeout:g}s", outcome="unknown" if exclusive else "none")
            except BridgeError as e:
                return err(e.code, str(e), outcome="unknown" if exclusive else "none")
        finally:
            self.inflight -= 1
            self.bridge.forget(rid)
            if creq:
                self.active.pop(creq, None)
        ms = {"queue": q_ms, "lr": int((time.monotonic() - t1) * 1000)}
        if rep.get("success"):
            return {"ok": True, "result": rep.get("result"), "ms": ms}
        e = rep.get("error") or {}
        if not isinstance(e, dict):
            e = {"message": str(e)}
        return {"ok": False, "error": {"code": e.get("code", "LR_ERROR"), "message": e.get("message", ""),
                                       "details": e.get("details") or {}}, "ms": ms}
