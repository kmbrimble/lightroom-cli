import io, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import quality_review as qr  # noqa: E402

pytest.importorskip("PIL")

OK = {"highlights": 1, "shadows": 0, "exposure": 0, "focus": 0, "motion_blur": 0, "noise": 1, "horizon_tilt_deg": -1.5,
      "tilt_confidence": 0.8, "keeper_score": 4, "fixes": ["straighten", "bogus"], "uncertain": False, "note": "ok"}
KEY = "sk-ant-SECRET123"


def serve(status=200, payload=None, calls=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if calls is not None:
                calls.append((self.headers.get("x-api-key"), body))
            out = json.dumps(payload if payload is not None else
                             {"content": [{"type": "tool_use", "name": "record_quality", "input": OK}],
                              "usage": {"input_tokens": 1000, "output_tokens": 200}, "stop_reason": "tool_use"}).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


@pytest.fixture
def prev(tmp_path):
    from PIL import Image
    d = tmp_path / "previews"; d.mkdir()
    for i in "abc":
        Image.new("RGB", (1600, 1000), (90, 90, 90)).save(d / f"{i}.jpg")
    return str(d)


def test_row_written_validated_and_downscaled(prev, tmp_path):
    calls = []
    srv, url = serve(calls=calls)
    out = str(tmp_path / "l.ndjson")
    n, spent, stop = qr.run_reviews(["a"], {}, prev, out, KEY, 9, url=url, log=lambda m: None)
    srv.shutdown()
    row = json.loads(open(out).read())
    assert (n, stop) == (1, False) and row["id"] == "a" and row["kind"] == "arw" and row["fixes"] == ["straighten"]
    assert calls[0][0] == KEY and calls[0][1]["max_tokens"] == 700
    from PIL import Image
    img = calls[0][1]["messages"][0]["content"][0]["source"]["data"]
    import base64
    assert max(Image.open(io.BytesIO(base64.b64decode(img))).size) == 1280


def test_resume_skips_existing(prev, tmp_path):
    srv, url = serve()
    out = str(tmp_path / "l.ndjson")
    qr.run_reviews(["a", "b"], {}, prev, out, KEY, 9, url=url, log=lambda m: None)
    n, *_ = qr.run_reviews(["a", "b", "c"], {}, prev, out, KEY, 9, url=url, log=lambda m: None)
    srv.shutdown()
    assert n == 1 and len(open(out).read().splitlines()) == 3


def test_budget_stop(prev, tmp_path):
    srv, url = serve()
    out = str(tmp_path / "l.ndjson")
    n, spent, stop = qr.run_reviews(["a", "b", "c"], {}, prev, out, KEY, 0.004, workers=1, url=url, log=lambda m: None)
    srv.shutdown()
    assert stop and n == 1 and spent == pytest.approx(0.004)


def test_error_text_scrubbed_and_retried(prev, tmp_path):
    calls, logs = [], []
    srv, url = serve(status=429, payload={"error": {"message": f"bad key {KEY}"}}, calls=calls)
    out = str(tmp_path / "l.ndjson")
    n, *_ = qr.run_reviews(["a"], {}, prev, out, KEY, 9, url=url, log=logs.append, sleep=lambda s: None)
    srv.shutdown()
    assert n == 0 and len(calls) == 5 and logs and all(KEY not in m for m in logs) and not os.path.exists(out)


def test_max_tokens_is_error(prev):
    srv, url = serve(payload={"content": [], "stop_reason": "max_tokens", "usage": {}})
    with pytest.raises(RuntimeError, match="max_tokens"):
        qr.review(os.path.join(prev, "a.jpg"), KEY, url=url, retries=1, sleep=lambda s: None)
    srv.shutdown()


def test_agree():
    a = [{"id": "1", **{k: 1 for k in qr.ORD + ["exposure", "keeper_score"]}, "horizon_tilt_deg": 1.0, "fixes": ["sharpen", "crop"]}]
    b = [{"id": "1", **{k: 2 for k in qr.ORD + ["exposure", "keeper_score"]}, "horizon_tilt_deg": 2.0, "fixes": ["sharpen"]}]
    r = qr.agree(a, b)
    assert r["focus"] == {"exact": 0.0, "within1": 1.0} and r["horizon_tilt_deg"]["mean_abs_diff"] == 1.0 and r["fixes_jaccard"] == 0.5


def test_build_set_unique_and_deterministic(tmp_path):
    rows = [{"id": str(i), "source": "x", "flags": [{"code": c, "severity": "warn"} for c in
            (["soft_focus", "blown_highlights"] if i % 3 == 0 else ["tilted_horizon"] if i % 3 == 1 else [])]} for i in range(900)]
    (tmp_path / "quality.ndjson").write_text("\n".join(map(json.dumps, rows)))
    (tmp_path / "final.ndjson").write_text("\n".join(json.dumps({"photo_id": str(i), "fields": {"scene_type": "lake"}}) for i in range(900)))
    a, b = qr.build_set(str(tmp_path), 1), qr.build_set(str(tmp_path), 1)
    ids = [p["id"] for p in a["photos"]]
    assert a == b and len(ids) == len(set(ids))


def test_retry_after_max_tokens_raises_limit(prev):
    calls, state = [], {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["max_tokens"])
            first = len(calls) == 1
            out = json.dumps({"content": [] if first else [{"type": "tool_use", "name": "record_quality", "input": OK}],
                              "stop_reason": "max_tokens" if first else "tool_use", "usage": {}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    f, _ = qr.review(os.path.join(prev, "a.jpg"), KEY, url=f"http://127.0.0.1:{srv.server_port}", sleep=lambda s: None)
    srv.shutdown()
    assert calls == [700, 1400] and f["keeper_score"] == 4
