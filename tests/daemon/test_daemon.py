import asyncio
import json
import os

from .conftest import FAST, Http, make_daemon
from .mock_plugin import MockPlugin


def audit_lines(d):
    p = os.path.join(d.home, "audit.jsonl")
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


# ---- basics + guard integration (design 8.3)
async def test_call_roundtrip_and_health(http, daemon):
    r = await http.call("system.ping")
    assert r["ok"] and r["result"]["pong"] and set(r["ms"]) == {"queue", "lr"}
    st, _, h = await http.raw("GET", "/v1/health")
    assert st == 200 and h["bridge"] == "connected" and h["daemon"] == "up"


async def test_dw_on_original_denied_and_not_forwarded(http, mock, daemon):
    mock.selection = [1]  # original
    r = await http.call("develop.setValue", {"parameter": "Exposure", "value": 1})
    assert not r["ok"] and r["error"]["code"] == "GUARD_DENIED"
    assert "createVirtualCopy" in r["error"]["details"]["hint"]
    assert not any(c == "develop.setValue" for c, _ in mock.received)
    a = audit_lines(daemon)[-1]
    assert a["decision"] == "deny" and a["command"] == "develop.setValue"


async def test_dw_on_vc_forwarded_by_selection_and_by_id(http, mock, daemon):
    mock.selection = [2]
    assert (await http.call("develop.setValue", {"parameter": "Exposure", "value": 1}))["ok"]
    assert (await http.call("develop.batchApplySettings", {"photoIds": [2, 3], "settings": {}}))["ok"]
    r = await http.call("develop.applySettings", {"photoId": 1, "settings": {}})
    assert r["error"]["code"] == "GUARD_DENIED" and r["error"]["details"]["offending"] == [1]
    r = await http.call("develop.batchSetValue", {"photoIds": [2, 1]})
    assert r["error"]["code"] == "GUARD_DENIED"
    assert [c for c, _ in mock.writes] == ["develop.setValue", "develop.batchApplySettings"]


async def test_dw_with_empty_selection_denied(http, mock):
    mock.selection = []
    assert (await http.call("develop.resetToDefault"))["error"]["code"] == "GUARD_DENIED"


async def test_library_writes_allowed_on_originals_no_flags(http, mock):
    mock.selection = [1]
    for c, p in [("catalog.addKeywords", {"photoId": 1, "keywords": ["x"]}), ("catalog.setRating", {"photoId": 1, "rating": 3}),
                 ("catalog.setMetadata", {"photoId": 1, "key": "anythingGoes", "value": 1}),
                 ("catalog.createCollection", {"name": "c"}), ("catalog.setFlag", {"photoId": 1, "flag": "pick"}),
                 ("catalog.createVirtualCopy", {})]:
        assert (await http.call(c, p))["ok"], c


async def test_remove_from_catalog_and_external_edit_need_flags(http, mock):
    r = await http.call("catalog.removeFromCatalog", {"photoId": 1})
    assert r["error"]["code"] == "GUARD_DENIED"
    assert (await http.call("catalog.removeFromCatalog", {"photoId": 1}, flags={"allow_remove_from_catalog": True}))["ok"]
    assert (await http.call("develop.editInPhotoshop"))["error"]["code"] == "GUARD_DENIED"
    mock.selection = [2]
    assert (await http.call("develop.editInPhotoshop", flags={"allow_external_edit": True}))["ok"]


async def test_unknown_and_disk_delete_denied_before_plugin(http, mock):
    for c in ("catalog.deleteFromDisk", "catalog.brandNew"):
        assert (await http.call(c, flags={"allow_remove_from_catalog": True}))["error"]["code"] == "GUARD_DENIED"
    assert mock.received == [c for c in mock.received if c[0] == "system.ping"]


async def test_audit_logs_nonreads_not_reads_and_rotates(http, daemon):
    await http.call("catalog.getAllPhotos")
    await http.call("catalog.setRating", {"photoId": 1, "rating": 2}, client="tester")
    a = audit_lines(daemon)
    assert [x["command"] for x in a] == ["catalog.setRating"]
    assert a[0]["client"] == "tester" and a[0]["outcome"] == "ok" and len(a[0]["params_digest"]) == 16
    assert "rating" not in json.dumps(a[0]["params_digest"])
    daemon.audit.max_bytes = 300
    for _ in range(6):
        await http.call("catalog.setRating", {"photoId": 1, "rating": 2})
    assert os.path.exists(daemon.audit.path + ".1")


async def test_plugin_error_passthrough(http, mock):
    mock.errors["catalog.getAllPhotos"] = "NOPE"
    r = await http.call("catalog.getAllPhotos")
    assert not r["ok"] and r["error"]["code"] == "NOPE"


# ---- scheduler (design 8.4)
async def test_lane_invariants_under_50_mixed_calls(http, mock, daemon):
    mock.latency = 0.02
    mock.selection = [2]
    reads = ["catalog.getAllPhotos", "catalog.getKeywords", "preview.getPreviewInfo", "catalog.getCollections"]
    writes = ["catalog.setRating", "catalog.addKeywords", "selection.nextPhoto", "develop.getSettings"]
    jobs = []
    for i in range(50):
        c = (reads if i % 3 else writes)[i % 4]
        jobs.append(http.call(c, {"photoId": 2, "n": i}))
    res = await asyncio.gather(*jobs)
    assert all(r["ok"] for r in res)
    assert mock.violations == []
    assert mock.max_r <= 4
    assert mock.max_r >= 2, "reads should actually overlap"


async def test_lane_m_is_fifo(http, mock):
    mock.latency = 0.01
    async def go(i):
        await asyncio.sleep(i * 0.003)  # staggered arrival
        return await http.call("catalog.setRating", {"photoId": 1, "rating": i % 6, "i": i})
    await asyncio.gather(*(go(i) for i in range(15)))
    order = [p["i"] for c, p in mock.received if c == "catalog.setRating"]
    assert order == sorted(order)


async def test_dw_lock_held_across_resolve_check_execute(http, mock):
    """Another client's setSelectedPhotos queued behind a DW cannot slip between check and execute."""
    mock.selection = [2]
    mock.latency = {"develop.setValue": 0.15}
    a = asyncio.create_task(http.call("develop.setValue", {"parameter": "Exposure", "value": 1}))
    await asyncio.sleep(0.05)
    b = asyncio.create_task(http.call("catalog.setSelectedPhotos", {"photoIds": [1]}))
    ra, rb = await asyncio.gather(a, b)
    assert ra["ok"] and rb["ok"]
    cmds = [c for c, _ in mock.received]
    assert cmds.index("develop.setValue") < cmds.index("catalog.setSelectedPhotos")
    assert mock.violations == []


# ---- deadlines (design 8.4/8.5)
async def test_deadline_sends_cancel_and_lr_timeout(http, mock):
    mock.drop.add("catalog.setRating")
    r = await http.call("catalog.setRating", {"photoId": 1, "rating": 1}, timeout=0.2)
    assert r["error"]["code"] == "LR_TIMEOUT" and r["error"]["details"]["outcome"] == "unknown"
    await asyncio.sleep(0.1)
    assert len(mock.cancelled) == 1


async def test_lane_held_until_late_reply_or_grace(http, mock, daemon):
    mock.hang.add("catalog.setRating")
    t0 = asyncio.get_running_loop().time()
    r1 = await http.call("catalog.setRating", {"photoId": 1}, timeout=0.15)  # hangs; cancel releases it
    assert r1["error"]["code"] == "LR_TIMEOUT"
    r2 = await http.call("catalog.getAllPhotos")  # R lane free again afterwards
    assert r2["ok"] and mock.violations == []


async def test_grace_expiry_frees_lane_when_plugin_never_answers(http, mock, daemon):
    mock.drop.add("catalog.setRating")
    t0 = asyncio.get_running_loop().time()
    await http.call("catalog.setRating", {"photoId": 1}, timeout=0.1)
    assert asyncio.get_running_loop().time() - t0 >= 0.1 + daemon.sched.grace - 0.05
    assert (await http.call("catalog.setFlag", {"photoId": 1}))["ok"]


async def test_cancel_endpoint_forwards(http, mock):
    mock.hang.add("catalog.setRating")
    t = asyncio.create_task(http.call("catalog.setRating", {"photoId": 1}, request_id="job1", timeout=5))
    await asyncio.sleep(0.1)
    st, _, o = await http.raw("POST", "/v1/cancel", {"requestId": "job1"})
    assert o["cancelled"] is True
    r = await asyncio.wait_for(t, 3)  # hang released by cancel, mock then replies
    assert r["ok"]
    assert len(mock.cancelled) == 1


async def test_batch_ordered_stop_on_error(http, mock):
    st, _, o = await http.raw("POST", "/v1/batch", {"calls": [
        {"command": "catalog.setRating", "params": {"photoId": 1}}, {"command": "bogus.cmd"},
        {"command": "catalog.setFlag", "params": {"photoId": 1}}]})
    assert [r["ok"] for r in o["results"]] == [True, False] and o["ok"] is False
    st, _, o = await http.raw("POST", "/v1/batch", {"ordered": False, "calls": [
        {"command": "system.ping"}, {"command": "catalog.getAllPhotos"}]})
    assert o["ok"] is True and len(o["results"]) == 2


# ---- resilience (design 8.5)
async def test_kill_mid_request_bridge_lost_then_reconnect(http, mock, daemon):
    mock.latency = {"catalog.setRating": 5}
    t = asyncio.create_task(http.call("catalog.setRating", {"photoId": 1}))
    await asyncio.sleep(0.1)
    mock.kill_connection()
    r = await asyncio.wait_for(t, 3)
    assert r["error"]["code"] == "BRIDGE_LOST" and r["error"]["details"]["outcome"] == "unknown"
    mock.latency = 0
    assert await daemon.bridge.wait_up(8)
    assert daemon.bridge.connects == 2
    assert (await http.call("system.ping", wait_bridge=5))["ok"]


async def test_read_in_flight_lost_says_no_outcome_risk(http, mock, daemon):
    mock.latency = {"catalog.getAllPhotos": 5}
    t = asyncio.create_task(http.call("catalog.getAllPhotos"))
    await asyncio.sleep(0.1)
    mock.kill_connection()
    r = await asyncio.wait_for(t, 3)
    assert r["error"]["code"] == "BRIDGE_LOST" and r["error"]["details"]["outcome"] == "none"


async def test_server_shutdown_event_drops_and_reconnects(mock, daemon):
    await mock.shutdown_event()
    await asyncio.sleep(0.1)
    assert daemon.bridge.last_event == "server.shutdown"
    assert await daemon.bridge.wait_up(8) and daemon.bridge.connects == 2


async def test_daemon_started_before_plugin_bridge_down_then_connects(tmp_path):
    d = make_daemon(tmp_path)
    await d.start()
    try:
        h = Http(d)
        r = await h.call("system.ping", wait_bridge=0)
        assert r["error"]["code"] == "BRIDGE_DOWN" and "Start CLI Bridge" in r["error"]["message"]
        m = MockPlugin(port_file=tmp_path / "ports.txt", restart_delay=0.1)
        await m.start()
        try:
            assert (await h.call("system.ping", wait_bridge=8))["ok"]
        finally:
            await m.stop()
    finally:
        await d.stop(drain=1)


async def test_lightroom_gone_entirely_then_back(mock, daemon, http, tmp_path):
    await mock.stop()
    await asyncio.sleep(0.3)
    assert (await http.call("system.ping", wait_bridge=0))["error"]["code"] == "BRIDGE_DOWN"
    await mock.start()
    assert await daemon.bridge.wait_up(8)


async def test_stale_port_file_does_not_wedge(tmp_path):
    (tmp_path / "ports.txt").write_text("1,2")
    d = make_daemon(tmp_path)
    await d.start()
    try:
        m = MockPlugin(port_file=None)
        ports = await m.start()
        (tmp_path / "ports.txt").write_text(f"{ports[0]},{ports[1]}")  # file refreshed later
        assert await d.bridge.wait_up(8)
        await m.stop()
    finally:
        await d.stop(drain=1)


async def test_busy_plugin_backlog_only_is_detected(tmp_path):
    """A second client is accepted by the kernel but never serviced -> handshake fails, daemon backs off."""
    m = MockPlugin(port_file=tmp_path / "ports.txt")
    ports = await m.start()
    r1, w1 = await asyncio.open_connection("127.0.0.1", ports[1])   # squatter takes the single slot
    r2, w2 = await asyncio.open_connection("127.0.0.1", ports[0])
    d = make_daemon(tmp_path, handshake_timeout=0.3)
    await d.start()
    try:
        for _ in range(40):
            if "handshake" in d.bridge.reason:
                break
            await asyncio.sleep(0.05)
        assert "handshake" in d.bridge.reason and d.bridge.state != "up"
        assert (await Http(d).call("system.ping", wait_bridge=0))["error"]["code"] == "BRIDGE_DOWN"
        assert m.received == []  # nothing was ever serviced for the daemon
    finally:
        w1.close(); w2.close()
        await d.stop(drain=1)
        await m.stop()


async def test_out_of_order_replies_matched_by_id(http, mock):
    mock.latency = {"catalog.getAllPhotos": 0.2, "catalog.getKeywords": 0.01}
    a, b = await asyncio.gather(http.call("catalog.getAllPhotos"), http.call("catalog.getKeywords"))
    assert a["result"]["echo"] == "catalog.getAllPhotos" and b["result"]["echo"] == "catalog.getKeywords"


async def test_late_reply_after_timeout_dropped_quietly(http, mock, daemon):
    mock.latency = {"catalog.getAllPhotos": 0.3}
    r = await http.call("catalog.getAllPhotos", timeout=0.1)
    assert r["error"]["code"] == "LR_TIMEOUT"
    await asyncio.sleep(0.4)
    assert (await http.call("system.ping"))["ok"] and daemon.bridge._pending == {}


async def test_client_disconnect_does_not_abort_guarded_write(http, mock, daemon):
    mock.selection = [2]
    mock.latency = {"develop.setValue": 0.2}
    r, w = await asyncio.open_connection("127.0.0.1", daemon.port)
    body = json.dumps({"command": "develop.setValue", "params": {}}).encode()
    w.write((f"POST /v1/call HTTP/1.1\r\nHost: 127.0.0.1:{daemon.port}\r\nX-Lrd-Token: {daemon.token}\r\n"
             f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
    await w.drain()
    await asyncio.sleep(0.05)
    w.close()
    await asyncio.sleep(0.5)
    assert [c for c, _ in mock.writes] == ["develop.setValue"]
    assert daemon.sched.inflight == 0 and audit_lines(daemon)[-1]["outcome"] == "ok"


# ---- security (design 8.6)
async def test_security_checks(http, daemon):
    assert (await http.raw("GET", "/v1/health", token=False))[0] == 401
    assert (await http.raw("GET", "/v1/health", token="wrong"))[0] == 401
    assert (await http.raw("GET", "/v1/health", host="evil.example:80"))[0] == 403
    assert (await http.raw("GET", "/v1/health", host=f"localhost:{daemon.port}"))[0] == 403
    assert (await http.raw("POST", "/v1/call", {"command": "system.ping"}, ctype="text/plain"))[0] == 415
    assert (await http.raw("POST", "/v1/call", {"command": "system.ping"}, ctype=None))[0] == 415
    st, hd, _ = await http.raw("OPTIONS", "/v1/call", headers={"Origin": "http://evil", "Access-Control-Request-Method": "POST"})
    assert st == 405 and not any(k.startswith("access-control") for k in hd)
    st, hd, _ = await http.raw("GET", "/v1/health")
    assert st == 200 and not any(k.startswith("access-control") for k in hd)
    assert (await http.raw("GET", "/nope"))[0] == 404
    assert (await http.raw("POST", "/v1/call", ["not", "object"]))[0] == 400


async def test_state_file_and_stop(http, daemon):
    p = os.path.join(daemon.home, "lrd.json")
    s = json.load(open(p))
    assert s["port"] == daemon.port and s["token"] == daemon.token and s["pid"] == os.getpid()
    if os.name == "posix":
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    st, _, o = await http.raw("POST", "/v1/shutdown", {})
    assert o["stopping"]
    await asyncio.wait_for(daemon.serve(), 5)
    assert not os.path.exists(p)
