import json, math, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import triage_classify  # noqa: E402
import triage_escalate  # noqa: E402
from lr_triage.cascade import Escalator, decide, escalation_reasons, load_quality  # noqa: E402
from lr_triage.classify import field_confidences, local_classify  # noqa: E402

FIELDS = {"scene_type": "lake", "has_people": False, "people_count": 0, "is_close_up_person": False,
          "visible_problems": ["none"], "subjects": ["lake"], "keywords": ["a", "b", "c", "d", "e"], "short_caption": "A lake."}


def tokenise(content, lp=-0.01, special=None):
    toks = [content[i:i + 3] for i in range(0, len(content), 3)]
    return [{"token": t, "logprob": (special or {}).get(t, lp), "top_logprobs": []} for t in toks]


class Server:
    def __init__(self, handler):
        self.srv = HTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def make_local(fields, special=None, calls=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if calls is not None:
                calls.append(body)
            c = json.dumps(fields)
            out = json.dumps({"choices": [{"message": {"content": c}, "logprobs": {"content": tokenise(c, special=special)}}],
                              "usage": {"prompt_tokens": 800}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    return H


def make_claude(calls):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            calls.append(({k.lower(): v for k, v in self.headers.items()}, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            out = json.dumps({"content": [{"type": "tool_use", "name": "record_triage", "input": {**FIELDS, "scene_type": "waterfall"}}],
                              "usage": {"input_tokens": 1000, "output_tokens": 200}, "stop_reason": "tool_use"}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    return H


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "previews"; p.mkdir()
    def mk(name):
        f = p / f"{name}.jpg"; f.write_bytes(b"\xff\xd8fake\xff\xd9"); os.utime(f, (1, 1)); return f
    mk.dir = p
    return mk


def test_field_confidences_path_probability():
    c = json.dumps(FIELDS)
    # make the token containing 'lake' (scene value) uncertain
    i = c.index('"lake"'); tok = c[(i // 3) * 3:(i // 3) * 3 + 3]
    conf = field_confidences(c, tokenise(c, special={tok: math.log(0.5)}))
    assert conf["scene_type"] < 0.6 and conf["has_people"] > 0.9 and conf["visible_problems"] > 0.9
    assert field_confidences(c, tokenise(c[:-1])) == {}  # tokens don't reassemble


def test_local_classify_and_quality_reasons(img):
    calls = []
    s = Server(make_local(FIELDS, calls=calls))
    try:
        r = local_classify(str(img("p1")), s.url)
    finally:
        s.close()
    assert r["fields"]["scene_type"] == "lake" and r["min_conf"] > 0.9
    assert calls[0]["response_format"]["type"] == "json_schema" and calls[0]["logprobs"] is True
    q = {"flags": [{"code": "soft_focus", "severity": "warn"}], "needs_second_opinion": {"value": False}}
    assert escalation_reasons(r, q) == ["quality_missed:soft_focus"]
    r["fields"] = {**FIELDS, "visible_problems": ["blown_highlights"]}
    assert set(escalation_reasons(r, {"flags": []})) == {"quality_contradicts:blown_highlights"}
    assert escalation_reasons(r, {"flags": [{"code": "overexposed", "severity": "bad"}]}) == []
    r["fields"] = {**FIELDS, "people_count": 1, "is_close_up_person": True}
    assert "close_up_person" in escalation_reasons(r, {"flags": [], "needs_second_opinion": {"value": True}})


def test_escalator_dry_budget_and_live(img):
    loc = {"fields": FIELDS, "conf": {"scene_type": 0.3, "has_people": 1, "visible_problems": 1}, "min_conf": 0.3}
    f = img("p1")
    rec = decide("p1", loc, {})
    assert rec["escalate"] and Escalator(None).run(rec, str(f))["escalation"] == "dry_run"
    calls = []; s = Server(make_claude(calls))
    try:
        e = Escalator("sk-test", max_usd=0.0025, url=s.url + "/v1/messages")
        r1 = e.run(rec, str(f)); r2 = e.run(rec, str(f))
    finally:
        s.close()
    assert r1["source"] == "claude" and r1["fields"]["scene_type"] == "waterfall" and r1["local"]["fields"]["scene_type"] == "lake"
    assert r1["claude"]["cost_usd"] == pytest.approx(0.004)  # 1000*2 + 200*10 per MTok
    assert r2["escalation"] == "budget_exhausted" and r2["source"] == "local" and len(calls) == 1
    hdrs, body = calls[0]
    assert hdrs["x-api-key"] == "sk-test" and body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 500
    assert body["tool_choice"] == {"type": "auto"}  # forced tool_choice 400s on Sonnet 5.5


def test_escalate_script_scrubs_key_and_resumes(img, tmp_path, monkeypatch):
    f = img("p1")
    loc = {"fields": FIELDS, "conf": {"scene_type": 0.3, "has_people": 1, "visible_problems": 1}, "min_conf": 0.3}
    tri = tmp_path / "t.ndjson"
    tri.write_text(json.dumps(decide("p1", loc, {})) + "\n" + json.dumps(decide("p2", {**loc, "conf": {"scene_type": 1, "has_people": 1, "visible_problems": 1}, "min_conf": 1}, {})) + "\n")
    calls = []; s = Server(make_claude(calls)); out = tmp_path / "final.ndjson"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-123")
    args = ["--triage", str(tri), "--previews", str(img.dir), "--out", str(out), "--api-url", s.url + "/v1/messages"]
    try:
        triage_escalate.main(args); triage_escalate.main(args)  # second run: nothing new
    finally:
        s.close()
    recs = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(calls) == 1 and [r["source"] for r in recs] == ["claude", "local"]
    from lr_triage.cascade import scrub
    assert "sk-secret-123" not in scrub("bad key sk-secret-123", "sk-secret-123")
    assert "sk-secret-123" not in out.read_text()


def test_driver_resume_and_report(img, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for n in ("a", "b"):
        img(n)
    (tmp_path / "q.ndjson").write_text(json.dumps({"source": "C:\\x\\a.jpg", "flags": [{"code": "soft_focus", "severity": "warn"}]}) + "\n")
    s = Server(make_local(FIELDS)); out = tmp_path / "t.ndjson"
    args = ["--previews", str(img.dir), "--quality", str(tmp_path / "q.ndjson"), "--out", str(out), "--url", s.url]
    try:
        triage_classify.main(args)
        n1 = len(out.read_text().splitlines())
        img("c")
        triage_classify.main(args)  # resume: only c is new
    finally:
        s.close()
    recs = [json.loads(l) for l in out.read_text().splitlines()]
    assert n1 == 2 and sorted(r["photo_id"] for r in recs) == ["a", "b", "c"]
    a = next(r for r in recs if r["photo_id"] == "a")
    assert a["escalate"] and a["source"] == "local"
    assert "Projected Sonnet cost" in (tmp_path / "t_report.md").read_text()
    assert load_quality(str(tmp_path / "q.ndjson")).keys() == {"a"}
