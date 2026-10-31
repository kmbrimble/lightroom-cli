#!/usr/bin/env python3
"""Write AI keywords and smart collections into Lightroom through the lrd daemon (stdlib only; run ON the desktop).

Only two write commands are ever sent: catalog.addKeywords and catalog.createSmartCollection. Everything else is a read.
Plan file (from scripts/triage_keywords.py): NDJSON {"photo_id","keywords":[...]}. See docs/triage-keywords.md.

    python triage_apply.py dry     --plan plan.ndjson [--live]
    python triage_apply.py canary  --plan plan.ndjson --log run.ndjson --ids 1,2,3,4,5
    python triage_apply.py run     --plan plan.ndjson --log run.ndjson
    python triage_apply.py collections --plan plan.ndjson [--only 'AI - Waterfall']
    python triage_apply.py verify  --plan plan.ndjson --log run.ndjson [--sample 200]
"""
import argparse
import collections
import importlib.util
import json
import os
import random
import sys
import time

CLIENT = "triage-apply"
MAX_ERR_RATE = 0.02
MIN_FOR_RATE = 50
WRITE_CMDS = {"catalog.addKeywords", "catalog.createSmartCollection"}

# (collection name, keyword). Substring-matched by Lightroom ('any' = contains), see docs.
COLLECTIONS = [("AI - Mountain", "mountain"), ("AI - Lake", "lake"), ("AI - Desert", "desert"), ("AI - Waterfall", "waterfall"),
               ("AI - Canyon", "canyon"), ("AI - Wildlife", "wildlife"), ("AI - Coast", "coast"), ("AI - Architecture", "architecture"),
               ("AI - Forest", "forest"), ("AI - Garden", "garden"), ("AI - City", "city"), ("AI - Indoor", "indoor"),
               ("AI - Portrait", "portrait"), ("AI - People", "people")]


def load_lrc(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "lrc.py")
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lightroom_sdk", "lrc.py")
    spec = importlib.util.spec_from_file_location("lrc_client", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Daemon:
    def __init__(self, lrc):
        self.lrc = lrc
        self.writes = 0

    def call(self, command, params=None, timeout=30):
        if command in WRITE_CMDS:
            self.writes += 1
        _, obj = self.lrc.request("POST", "/v1/call", {"command": command, "params": params or {}, "timeout": timeout,
                                                       "client": CLIENT}, timeout + 5)
        return obj

    def healthy(self):
        _, obj = self.lrc.request("GET", "/v1/health", None, 5)
        return bool(obj.get("ok")) and obj.get("bridge") == "connected"


def read_ndjson(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def append(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
        f.flush()
        os.fsync(f.fileno())


def kw_names(meta):
    return [k["name"] if isinstance(k, dict) else k for k in (meta.get("keywords") or [])]


def counts(plan):
    return collections.Counter(k for p in plan for k in p["keywords"])


def dry(plan, d=None):
    c = counts(plan)
    print(f"photos: {len(plan)}  distinct keywords: {len(c)}")
    for k, v in c.most_common():
        print(f"{v:5d}  {k}")
    rnd = random.Random(1)
    print("sample photos:")
    for p in rnd.sample(plan, min(10, len(plan))):
        print(" ", p["photo_id"], ", ".join(p["keywords"]))
    if d:
        r = d.call("catalog.getKeywords", timeout=120)
        have = {k["name"].lower(): k.get("photoCount") for k in (r.get("result") or {}).get("keywords", [])}
        print("existing catalog keywords that collide (name: photos already carrying):")
        for k in c:
            if k.lower() in have:
                print(f"  {k}: {have[k.lower()]}")
        print(f"(catalog has {len(have)} keywords before the run)")


def write_photo(d, log, entry, check=False, prior=()):
    """One photo: pre-read, intent line, addKeywords (retry once), ok/error line. Returns True on success."""
    pid, want = entry["photo_id"], entry["keywords"]
    pre = d.call("catalog.getPhotoMetadata", {"photoId": int(pid)})
    if not pre.get("ok"):
        append(log, {"photo_id": pid, "status": "error", "stage": "pre-read", "error": pre.get("error")})
        return False, pre
    have = {k.lower() for k in kw_names(pre["result"])}
    # a crashed earlier attempt may already have added some: keep them in this photo's undo record
    to_add = [k for k in want if k.lower() not in have or k in prior]
    append(log, {"photo_id": pid, "status": "start", "to_add": to_add, "preexisting": [k for k in want if k.lower() in have], "ts": time.time()})
    r = None
    for _ in range(2):
        r = d.call("catalog.addKeywords", {"photoId": int(pid), "keywords": to_add or [want[-1]]}, timeout=30)
        if r.get("ok"):
            break
        if (r.get("error") or {}).get("code") in ("BRIDGE_DOWN", "DAEMON_DOWN", "GUARD_DENIED"):
            break
    if not r.get("ok"):
        append(log, {"photo_id": pid, "status": "error", "stage": "add", "error": r.get("error")})
        return False, r
    if check:
        post = d.call("catalog.getPhotoMetadata", {"photoId": int(pid)})
        if not post.get("ok"):
            append(log, {"photo_id": pid, "status": "error", "stage": "canary-readback", "error": post.get("error")})
            return False, post
        pm, om = post["result"], pre["result"]
        diff = {k for k in set(pm) | set(om) if k not in ("keywords", "collections") and pm.get(k) != om.get(k)}
        got = {k.lower() for k in kw_names(pm)}
        missing = [k for k in want if k.lower() not in got]
        extra = sorted(got - have - {k.lower() for k in want})
        if diff or missing or extra:
            append(log, {"photo_id": pid, "status": "error", "stage": "canary-check", "other_metadata_changed": sorted(diff),
                         "missing": missing, "unexpected_new": extra})
            return False, {"error": {"code": "CANARY_MISMATCH", "message": f"diff={sorted(diff)} missing={missing} extra={extra}"}}
    append(log, {"photo_id": pid, "status": "ok", "added": to_add})
    return True, r


def run(d, plan, log, ids=None, check=False, sleep=0.0, limit=None):
    done = {e["photo_id"] for e in read_ndjson(log) if e["status"] == "ok"}
    todo = [p for p in plan if p["photo_id"] not in done and (ids is None or p["photo_id"] in ids)]
    todo = todo[:limit]
    print(f"{len(done)} already done, {len(todo)} to do this call")
    if not any(e["status"] == "baseline" for e in read_ndjson(log)):  # keyword photo counts before our first write
        r = d.call("catalog.getKeywords", timeout=120)
        if not r.get("ok"):
            print("ABORT: cannot read baseline keyword counts"); return 2
        append(log, {"status": "baseline", "counts": {k["name"].lower(): k.get("photoCount", 0) for k in r["result"]["keywords"]}})
    ok = err = consec = 0
    prior = collections.defaultdict(set)
    for e in read_ndjson(log):
        if e["status"] == "start":
            prior[e["photo_id"]].update(e["to_add"])
    for i, p in enumerate(todo, 1):
        if i % 100 == 1 and not d.healthy():
            print("ABORT: daemon/bridge not healthy"); return 2
        good, r = write_photo(d, log, p, check, prior[p["photo_id"]])
        if good:
            ok += 1; consec = 0
        else:
            err += 1; consec += 1
            code = (r.get("error") or {}).get("code")
            print(f"error photo {p['photo_id']}: {code} {(r.get('error') or {}).get('message')}")
            if code in ("BRIDGE_DOWN", "DAEMON_DOWN", "GUARD_DENIED", "CANARY_MISMATCH") or consec >= 5:
                print("ABORT: fatal/consecutive errors"); return 2
        if ok + err >= MIN_FOR_RATE and err / (ok + err) > MAX_ERR_RATE:
            print(f"ABORT: error rate {err}/{ok + err} > {MAX_ERR_RATE:.0%}"); return 2
        if i % 50 == 0:
            print(f"progress {i}/{len(todo)} ok={ok} err={err}", flush=True)
        if sleep:
            time.sleep(sleep)
    print(f"finished ok={ok} err={err}")
    return 0 if err == 0 else 1


def expected_for(plan, log):
    done = {e["photo_id"] for e in read_ndjson(log) if e["status"] == "ok"}
    return [p for p in plan if p["photo_id"] in done]


def make_collections(d, plan, only=None):
    c = counts(plan)
    existing = {x["name"] for x in (d.call("catalog.getCollections", {"includePhotoCounts": True}, 120).get("result") or {}).get("collections", [])}
    for name, kw in COLLECTIONS:
        if only and kw != only:
            continue
        if name in existing:
            print(f"exists, skipped: {name}"); continue
        sd = {"criteria": "keywords", "operation": "any", "value": kw}
        r = d.call("catalog.createSmartCollection", {"name": name, "searchDesc": sd}, 30)
        print(name, "->", "ok" if r.get("ok") else r.get("error"))
        if not r.get("ok"):
            return 1
        if only is None and name == COLLECTIONS[0][0]:
            pass
    return 0


def collection_report(d, plan, only=None, exact=False):
    """Lightroom's 'any' operator is a contains-match, so expected = photos with any keyword containing the word."""
    got = {x["name"]: x for x in (d.call("catalog.getCollections", {"includePhotoCounts": True}, 120).get("result") or {}).get("collections", [])}
    bad = 0
    for name, kw in COLLECTIONS:
        if only and kw != only:
            continue
        exp = sum(1 for p in plan if any(kw.lower() in k.lower() for k in p["keywords"]))
        n = (got.get(name) or {}).get("photoCount")
        tot = None
        if name in got:  # second opinion: getCollectionPhotos total
            tot = (d.call("catalog.getCollectionPhotos", {"collectionId": got[name]["id"], "limit": 1}, 120).get("result") or {}).get("total")
        flag = "" if n == exp and tot in (None, exp) else "  <-- MISMATCH"
        name = f"{name} [getCollectionPhotos total={tot}]"
        bad += flag != ""
        print(f"{name}: collection={n} expected={exp}{flag}")
    return bad


def verify(d, plan, log, sample):
    done = expected_for(plan, log)
    entries = read_ndjson(log)
    base = next((e["counts"] for e in entries if e["status"] == "baseline"), {})
    per_photo = collections.defaultdict(set)  # union: overlapping writers can log the same photo twice
    for e in entries:
        if e["status"] == "start":
            per_photo[e["photo_id"]].update(k.lower() for k in e["to_add"])
    added = collections.Counter(k for ks in per_photo.values() for k in ks)
    c = counts(done)
    r = d.call("catalog.getKeywords", timeout=120)
    have = {k["name"].lower(): k.get("photoCount") for k in (r.get("result") or {}).get("keywords", [])}
    bad = 0
    for k, v in c.most_common():
        n, want = have.get(k.lower()), base.get(k.lower(), 0) + added[k.lower()]  # photos already carrying it + ours
        if n != want:
            bad += 1
            print(f"MISMATCH keyword {k}: catalog={n} expected={want} (baseline {base.get(k.lower(), 0)} + added {added[k.lower()]})")
    print(f"keyword counts checked: {len(c)} ({bad} mismatches)")
    rnd = random.Random(7)
    smp = rnd.sample(done, min(sample, len(done)))
    miss = 0
    for p in smp:
        m = d.call("catalog.getPhotoMetadata", {"photoId": int(p["photo_id"])})
        got = {k.lower() for k in kw_names(m.get("result") or {})}
        gone = [k for k in p["keywords"] if k.lower() not in got]
        if gone:
            miss += 1; print(f"photo {p['photo_id']} missing {gone}")
    print(f"sampled {len(smp)} photos: {miss} with missing keywords")
    cb = collection_report(d, done)
    return 0 if bad == 0 and miss == 0 and cb == 0 else 1


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("mode", choices=["dry", "canary", "run", "collections", "verify", "report"])
    a.add_argument("--plan", required=True)
    a.add_argument("--log", default="triage_apply_log.ndjson")
    a.add_argument("--ids")
    a.add_argument("--live", action="store_true")
    a.add_argument("--only")
    a.add_argument("--sample", type=int, default=200)
    a.add_argument("--lrc")
    a.add_argument("--limit", type=int)
    a.add_argument("--sleep", type=float, default=0.0)
    o = a.parse_args(argv)
    plan = read_ndjson(o.plan)
    if o.mode == "dry" and not o.live:
        return dry(plan)
    d = Daemon(load_lrc(o.lrc))
    if not d.healthy():
        print("daemon not healthy / bridge not connected"); return 3
    if o.mode == "dry":
        return dry(plan, d)
    if o.mode == "canary":
        return run(d, plan, o.log, set(o.ids.split(",")), check=True)
    if o.mode == "run":
        return run(d, plan, o.log, sleep=o.sleep, limit=o.limit)
    if o.mode == "collections":
        return make_collections(d, plan, o.only)
    if o.mode == "report":
        return collection_report(d, expected_for(plan, o.log), o.only)
    return verify(d, plan, o.log, o.sample)


if __name__ == "__main__":
    sys.exit(main())
