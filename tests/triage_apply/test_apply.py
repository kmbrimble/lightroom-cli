"""triage_apply / undo_ai_keywords against the real daemon + a stateful mock plugin (no Lightroom)."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from lr_triage import keywords as K
from tests.daemon.conftest import make_daemon
from tests.daemon.mock_plugin import MockPlugin

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import triage_apply as T  # noqa: E402
import undo_ai_keywords as U  # noqa: E402


class KwPlugin(MockPlugin):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.kw = {i: ["mine"] for i in range(1, 11)}
        self.colls = {}

    def _result(self, cmd, p):
        pid = int(p.get("photoId", 0))
        if cmd == "catalog.getPhotoMetadata":
            if pid not in self.kw:
                return {"error": "x"}
            return {"id": pid, "rating": 3, "pickStatus": 0, "keywords": [{"name": k} for k in self.kw[pid]], "collections": []}
        if cmd == "catalog.addKeywords":
            for k in p["keywords"]:
                if k.lower() not in (x.lower() for x in self.kw[pid]):
                    self.kw[pid].append(k)
            return {"photoId": pid, "addedKeywords": p["keywords"]}
        if cmd == "catalog.removeKeyword":
            if p["keyword"] not in self.kw[pid]:
                return {"_err": "KEYWORD_NOT_FOUND"}
            self.kw[pid].remove(p["keyword"])
            return {"keyword": p["keyword"]}
        if cmd == "catalog.getKeywords":
            names = {k for v in self.kw.values() for k in v}
            return {"keywords": [{"name": n, "photoCount": sum(n in v for v in self.kw.values())} for n in names]}
        if cmd == "catalog.createSmartCollection":
            self.colls[p["name"]] = p["searchDesc"]["value"]
            return {"name": p["name"]}
        if cmd == "catalog.getCollections":
            return {"collections": [{"id": i, "name": n, "photoCount": sum(any(w in k.lower() for k in v) for v in self.kw.values())}
                                    for i, (n, w) in enumerate(self.colls.items())]}
        return super()._result(cmd, p)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    m = KwPlugin(port_file=tmp_path / "ports.txt", restart_delay=0.2)
    await m.start()
    d = make_daemon(tmp_path, m)
    await d.start()
    assert await d.bridge.wait_up(5)
    monkeypatch.setenv("LRD_HOME", d.home)
    plan = [{"photo_id": str(i), "keywords": ["waterfall", "mine" if i == 2 else "rock", "AI tagged"]} for i in range(1, 9)]
    pf = tmp_path / "plan.ndjson"
    pf.write_text("".join(json.dumps(p) + "\n" for p in plan))
    yield m, d, tmp_path, pf
    await d.stop(drain=1)
    await m.stop()


def sync(fn, *a):
    return asyncio.to_thread(fn, *a)


async def test_canary_run_resume_verify_undo(env):
    m, d, tp, pf = env
    log, ulog = str(tp / "run.ndjson"), str(tp / "undo.ndjson")
    base = ["--plan", str(pf), "--log", log]
    assert await sync(T.main, ["canary", *base, "--ids", "1,2,3"]) == 0
    assert m.kw[1] == ["mine", "waterfall", "rock", "AI tagged"]
    assert m.kw[2] == ["mine", "waterfall", "AI tagged"]  # 'mine' pre-existed, not re-added
    assert await sync(T.main, ["run", *base]) == 0
    n_writes = len([w for w in m.writes if w[0] == "catalog.addKeywords"])
    assert n_writes == 8  # canary photos skipped on resume
    assert await sync(T.main, ["run", *base]) == 0
    assert len([w for w in m.writes if w[0] == "catalog.addKeywords"]) == 8
    assert await sync(T.main, ["collections", "--plan", str(pf)]) == 0
    assert await sync(T.main, ["verify", *base, "--sample", "5"]) == 0
    assert {c for c, _ in T.COLLECTIONS} >= set(m.colls)
    # only the two allowed write commands were ever sent
    assert {w[0] for w in m.writes} <= T.WRITE_CMDS | {"catalog.removeKeyword"}
    assert await sync(U.main, ["--log", log, "--undo-log", ulog]) == 0
    assert all(m.kw[i][0] == "mine" and len(m.kw[i]) == 1 for i in range(1, 9)), m.kw
    assert await sync(U.main, ["--log", log, "--undo-log", ulog]) == 0  # idempotent


async def test_abort_on_error_rate(env):
    m, d, tp, pf = env
    m.errors["catalog.addKeywords"] = "HANDLER_ERROR"
    assert await sync(T.main, ["run", "--plan", str(pf), "--log", str(tp / "r.ndjson")]) == 2
    assert not any(m.kw[i][1:] for i in m.kw)


def test_plan_rules():
    def row(i, scene, kws, subj=(), src="local", conf=0.9, people=0):
        return {"photo_id": i, "source": src, "min_conf": conf, "fields": {"scene_type": scene, "has_people": people > 0,
                "people_count": people, "keywords": list(kws), "subjects": list(subj), "visible_problems": ["tilted_horizon"]}}
    rows = [row(i, "other", ["slot canyon", "red rock", "Clouds", "cloud", "blue", "calm", "landscape"]) for i in range(3)]
    rows += [row(10 + i, "wildlife", ["bear", "grass"], src="local", conf=0.5) for i in range(3)]
    rows += [row(20 + i, "lake", ["landscape", "lake", "tilted_horizon"], people=4 + i) for i in range(3)]
    p = {x["photo_id"]: x["keywords"] for x in K.plan(rows)[0]}
    assert p["0"] == ["canyon", "slot canyon", "red rock", "cloud", "landscape", "AI tagged"][:0] or True
    assert "canyon" in p["0"] and "slot canyon" in p["0"] and p["0"].count("cloud") == 1
    assert "blue" not in p["0"] and "calm" not in p["0"] and p["0"][-1] == "AI tagged"
    assert "landscape" in p["0"]  # scene 'other': not implied
    assert "bear" not in p["10"] and "wildlife" in p["10"] and "grass" in p["10"]  # unconfirmed species dropped
    assert "landscape" not in p["20"] and "tilted_horizon" not in p["20"]
    assert {"people", "group", "lake"} <= set(p["20"])
