import csv
import importlib.util
import json
import os
import re

import pytest

from lightroom_sdk.client import LightroomClient
from .catalog_mock import CatalogMock, make_fixture

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "catalog_cleanup.py")
spec = importlib.util.spec_from_file_location("catalog_cleanup", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)
REAL = "/projects/.lr-scratch/analysis"


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "csv"
    d.mkdir()
    exp, photos = make_fixture(str(d))
    return d, exp, photos, tmp_path


async def go(env, *argv, mock_kw=None, setup=None):
    d, exp, photos, tmp = env
    mock = CatalogMock(photos, port_file=tmp / "ports.txt", restart_delay=0.2)
    await mock.start()
    if setup:
        setup(mock)
    out = []
    try:
        a = cc.parse(["--csv-dir", str(d), "--log", str(tmp / "log.ndjson"), "--port-file", str(tmp / "ports.txt"), *argv])
        a.expected = exp
        rc = await cc.run(a, client=LightroomClient(port_file=str(tmp / "ports.txt")), out=out.append,
                          backoff=(0.05,) * 8)
    finally:
        await mock.stop()
    return rc, "\n".join(out), mock


def lines(tmp):
    p = tmp / "log.ndjson"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- plan / assertions
def test_real_csvs_match_verified_numbers():
    if not os.path.isdir(REAL):
        pytest.skip("analysis CSVs not present")
    p = cc.load_plan(REAL)
    assert (len(p.sources), len(p.sets), len(p.dups), len(p.kept)) == (4458, 949, 26, 84)
    assert p.after_a == 5981 and p.after_b == 1523


def test_tampered_csv_aborts(env):
    d, exp, photos, tmp = env
    p = d / "bracket_sets.csv"
    rows = list(csv.DictReader(open(p)))
    orig = [dict(r) for r in rows]

    def save(rs):
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, list(orig[0])); w.writeheader(); w.writerows(rs)

    def tamper(fn):
        rs = [dict(r) for r in orig]; fn(rs); save(rs)
        with pytest.raises(cc.Abort) as e:
            cc.load_plan(str(d), exp)
        assert e.value.code == 2 and "PLAN ASSERTION FAILED" in str(e.value)
    tamper(lambda rs: rs.pop())                                                  # missing set
    tamper(lambda rs: rs[0].update(source_ids=" ".join(rs[0]["source_ids"].split()[:-1]), n_frames=2))  # lost a source
    tamper(lambda rs: rs[1].update(kw="Focus bracket"))                          # focus keyword
    tamper(lambda rs: rs[1].update(source_ids=rs[0]["source_ids"]))              # shared source ids
    save(orig)
    cc.load_plan(str(d), exp)                                                    # untouched copy still passes
    with pytest.raises(cc.Abort):
        cc.load_plan(str(d), dict(exp, sources=exp["sources"] + 1))              # wrong expected number


async def test_full_rehearsal_on_real_csvs(tmp_path):
    if not os.path.isdir(REAL):
        pytest.skip("analysis CSVs not present")
    photos = {int(r["id"]): r["filename"] for r in csv.DictReader(open(f"{REAL}/hdr_outputs.csv"))}
    for r in csv.DictReader(open(f"{REAL}/bracket_sets.csv")):
        photos.update({int(i): f"NA{n}.ARW" for i, n in zip(r["source_ids"].split(), r["file_numbers"].split())})
    photos.update({5000000 + n: f"NA9{n:05d}.ARW" for n in range(487)})          # plain single ARWs
    photos.update({5001000 + j: n for j, n in enumerate(cc.EXPECTED["focus"])})  # focus-bracket frames
    assert len(photos) == 6007
    mock = CatalogMock(photos, port_file=tmp_path / "p.txt")
    await mock.start()
    try:
        a = cc.parse(["--csv-dir", REAL, "--step", "all", "--yes", "--log", str(tmp_path / "l.ndjson")])
        rc = await cc.run(a, client=LightroomClient(port_file=str(tmp_path / "p.txt")), out=lambda *_: None)
    finally:
        await mock.stop()
    assert rc == 0 and len(mock.removed) == 26 + 4458 and len(mock.cat) == 1523 == len(mock.cleared)


# ---- runs
async def test_dry_run_writes_nothing(env):
    rc, out, mock = await go(env, "--step", "all")                              # no --yes => dry run
    assert rc == 0 and mock.writes == [] and "DRY RUN" in out and "preflight ok" in out
    assert f"A remove -HDR-2.dng    : {env[1]['groups']}" in out and f"{env[1]['sources']} from 6 merged sets" in out
    assert not (env[3] / "log.ndjson").exists()
    rc, out, mock = await go(env, "--step", "all", "--yes", "--dry-run")        # --dry-run wins over --yes
    assert rc == 0 and mock.writes == []


async def test_all_steps_happy_path(env):
    d, exp, photos, tmp = env
    rc, out, mock = await go(env, "--step", "all", "--yes")
    assert rc == 0, out
    assert len(mock.removed) == exp["groups"] + exp["sources"]
    assert len(mock.cat) == exp["total"] - exp["groups"] - exp["sources"]
    names = set(mock.cat.values())
    assert not any(n.endswith("-HDR-2.dng") for n in names) and set(exp["focus"]) <= names
    assert sorted(mock.cleared) == sorted(mock.cat) and all(len(c[1]["photoIds"]) <= 25 for c in mock.received if c[0] == "develop.clearAutoTone")
    steps = [r["step"] for r in lines(tmp)]
    assert steps.count("A") == 1 and steps.count("B") == exp["sources"] and steps.count("C") == len(mock.cat)
    assert all(set(r) >= {"ts", "step", "id", "filename", "action", "result"} for r in lines(tmp))
    assert f"POSTFLIGHT live catalog: {len(mock.cat)} photos (expected {len(mock.cat)}" in out
    # rerun does nothing
    rc, out, m2 = await go((d, exp, mock.cat, tmp), "--step", "all", "--yes")
    assert rc == 0 and m2.writes == [] and m2.removed == []


async def test_canary_then_resume_and_order(env):
    d, exp, photos, tmp = env
    rc, out, _ = await go(env, "--step", "B", "--yes")                          # A not done
    assert rc == 2 and "refused" in out
    rc, out, _ = await go(env, "--step", "C", "--yes")
    assert rc == 2 and "refused" in out
    rc, out, m1 = await go(env, "--step", "all", "--yes", "--limit", "3")
    assert rc == 0 and len(m1.removed) == 1 + 3 and m1.cleared == [] and "step C skipped" in out
    rc, out, m2 = await go((d, exp, m1.cat, tmp), "--step", "all", "--yes")
    assert rc == 0, out
    assert len(m2.cat) == exp["total"] - exp["groups"] - exp["sources"]
    assert m2.removed and not set(m2.removed) & set(m1.removed)


async def test_photo_not_found_counts_as_done(env):
    d, exp, photos, tmp = env
    target = next(i for i, n in photos.items() if n.endswith(".ARW") and i < 1010)
    rc, out, mock = await go(env, "--step", "all", "--yes", setup=lambda m: m.gone_on_remove.add(target))
    assert rc == 0, out
    res = {r["id"]: r["result"] for r in lines(tmp) if r["step"] == "B"}
    assert res[target] == "already_gone" and list(res.values()).count("removed") == exp["sources"] - 1


async def test_resumed_run_tolerates_vanished_pending_photo(env):
    d, exp, photos, tmp = env
    _, _, m1 = await go(env, "--step", "all", "--yes", "--limit", "2")
    pending = next(i for i in sorted(m1.cat) if m1.cat[i].endswith(".ARW") and i in {int(x) for r in csv.DictReader(open(d / "bracket_sets.csv")) if int(r["n_merges"]) for x in r["source_ids"].split()})
    cat = {i: n for i, n in m1.cat.items() if i != pending}
    rc, out, m2 = await go((d, exp, cat, tmp), "--step", "all", "--yes")
    assert rc == 0, out
    assert {r["id"]: r["result"] for r in lines(tmp) if r["step"] == "B"}[pending] == "already_gone"


async def test_connection_drop_mid_run_resumes_on_same_client(env):
    d, exp, photos, tmp = env
    rc, out, mock = await go(env, "--step", "all", "--yes", setup=lambda m: setattr(m, "drop_after_removes", 6))
    assert rc == 0, out
    assert mock.restarts >= 1 and len(mock.cat) == exp["total"] - exp["groups"] - exp["sources"]
    assert len(mock.removed) == len(set(mock.removed)) == exp["groups"] + exp["sources"]


async def test_step_c_unknown_command_stops_cleanly(env):
    d, exp, photos, tmp = env
    rc, out, mock = await go(env, "--step", "all", "--yes", setup=lambda m: setattr(m, "no_clear", True))
    assert rc == 3 and "step C NOT AVAILABLE" in out
    assert len(mock.cat) == exp["total"] - exp["groups"] - exp["sources"]       # A and B completed
    assert not [r for r in lines(tmp) if r["step"] == "C"]


async def test_step_c_skipped_reason_logged(env):
    d, exp, photos, tmp = env
    rc, out, mock = await go(env, "--step", "all", "--yes",
                             setup=lambda m: m.clear_skip.update({i: "not a virtual copy" for i in list(m.cat)[-2:]}))
    c = [r for r in lines(tmp) if r["step"] == "C"]
    assert rc == 0 and sum(r["result"] == "skipped" for r in c) == 2 and all(r["skippedReason"] for r in c if r["result"] == "skipped")


async def test_preflight_mismatch_aborts_before_any_write(env):
    d, exp, photos, tmp = env
    victim = next(i for i, n in photos.items() if n.endswith(".ARW"))
    rc, out, mock = await go(env, "--step", "all", "--yes", setup=lambda m: m.cat.__setitem__(victim, "OTHER.ARW"))
    assert rc == 2 and "PREFLIGHT" in out and mock.writes == []
    rc, out, mock = await go(env, "--step", "all", "--yes", setup=lambda m: m.cat.__setitem__(99999, "extra.ARW"))
    assert rc == 2 and "expected" in out and mock.writes == []                  # wrong total on a fresh run


# ---- hard safety: nothing in the script can touch files on disk
def test_script_has_no_disk_delete_paths():
    src = open(SCRIPT, encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    banned = r"os\.remove|os\.unlink|os\.rmdir|os\.removedirs|os\.replace|os\.rename|shutil|rmtree|\.unlink\(|\.rmdir\(|\.rename\(|send2trash|subprocess|os\.system|\.truncate\("
    assert not re.search(banned, code), re.search(banned, code)
    assert not re.search(r"\bimport (shutil|pathlib|subprocess)|from (shutil|pathlib|subprocess)", code)
    opens = [l for l in code.splitlines() if re.search(r"\bopen\(", l)]
    assert len(opens) == 3 and not [l for l in opens if re.search(r'"[wx+]b?"|"r\+', l)], opens  # read, read, append-only log
    assert cc.ALLOWED == {"system.ping", "catalog.getAllPhotos", "catalog.getPhotoMetadata",
                          "catalog.removeFromCatalog", "develop.clearAutoTone"}
    cmds = set(re.findall(r'"((?:catalog|develop|system)\.\w+)"', code))
    assert cmds <= cc.ALLOWED, cmds - cc.ALLOWED
