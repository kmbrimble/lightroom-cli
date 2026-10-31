#!/usr/bin/env python3
"""Ground-truth image-quality labels from Claude (see lr_quality/rubric.md, docs/quality-labels.md).

  quality_review.py build-set [--seed N]
  hl exec --secret ANTHROPIC_API_KEY=claude-code/anthropic_api_key -- python scripts/quality_review.py review [--limit N] [--repeat K]
  quality_review.py agree
The key is read from the environment only; it is never printed, logged, written or put on argv. The prompt never sees
lr_quality output, scene labels or anything but the picture.
"""
import argparse, base64, collections, csv, io, json, os, random, sys, threading, time, urllib.error
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lr_triage.cascade import API_URL, MODEL, cost_usd, scrub  # noqa: E402
from lr_triage.classify import _post, parse_json  # noqa: E402

BASE = "/projects/.lr-scratch/quality-labels"
HDR_CSV = "/projects/.lr-scratch/analysis/hdr_outputs.csv"
FIXES = ["lower_highlights", "lift_shadows", "raise_exposure", "lower_exposure", "straighten", "sharpen", "crop", "none"]
ORD = ["highlights", "shadows", "focus", "motion_blur", "noise"]
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["highlights", "shadows", "exposure", "focus", "motion_blur", "noise", "horizon_tilt_deg",
                       "tilt_confidence", "keeper_score", "fixes", "uncertain", "note"],
          "properties": {
              "highlights": {"type": "integer"}, "shadows": {"type": "integer"}, "exposure": {"type": "integer"},
              "focus": {"type": "integer"}, "motion_blur": {"type": "integer"}, "noise": {"type": "integer"},
              "horizon_tilt_deg": {"type": ["number", "null"]}, "tilt_confidence": {"type": "number"},
              "keeper_score": {"type": "integer"},
              "fixes": {"type": "array", "items": {"type": "string", "enum": FIXES}},
              "uncertain": {"type": "boolean"}, "note": {"type": "string"}}}
PROMPT = (
    "You are an experienced landscape/travel photographer reviewing one photo (a 1280px preview of a Sony A7CR frame, "
    "either an HDR bracket merge or a single frame) and deciding what, if anything, needs fixing in Lightroom. "
    "Be calibrated, not harsh: most good travel photos have small flaws that do not matter. Ignore deliberate artistic "
    "choices (silhouettes, low-key/high-key mood, sun in frame; small flares are minor). Judge only what you can see.\n"
    "Fields:\n"
    "- highlights 0-3: blown/clipped bright areas. 0 none; 1 minor clipped areas that are not distracting; "
    "2 noticeable/distracting (e.g. large blown sky or bright area); 3 severe.\n"
    "- shadows 0-3: crushed/blocked-up shadow detail that matters to the picture (0 none ... 3 severe). Dark areas that are "
    "naturally black and unimportant score 0.\n"
    "- exposure -3..3: overall brightness. -3 far too dark, 0 right, +3 far too bright.\n"
    "- focus 0-3: 0 sharp where it matters; 1 slightly soft; 2 clearly soft/out of focus; 3 unusable. Shallow depth of field "
    "that is intentional is not a defect.\n"
    "- motion_blur 0-3: camera shake or subject blur (0 none ... 3 severe).\n"
    "- noise 0-3: visible noise/grain at this size (0 none ... 3 severe).\n"
    "- horizon_tilt_deg: number of degrees the image must be rotated CLOCKWISE to level the horizon or the dominant level "
    "line. A horizon that rises toward the right needs a positive value; one that descends toward the right needs a negative "
    "value. Use null if there is no reliable horizon or level line (mountains, forest, close-ups); 0 when level. One decimal.\n"
    "- tilt_confidence 0-1: how sure you are of horizon_tilt_deg (0 when null).\n"
    "- keeper_score 1-5: overall technical keeper rating (5 excellent, 1 reject).\n"
    "- fixes: subset of " + ", ".join(FIXES) + " that a photographer would actually apply; [\"none\"] if nothing.\n"
    "- uncertain: true if you are unsure about any field.\n"
    "- note: one short line.\n"
    "Call the record_quality tool with your answer."
)
RANGES = {"highlights": (0, 3), "shadows": (0, 3), "focus": (0, 3), "motion_blur": (0, 3), "noise": (0, 3),
          "exposure": (-3, 3), "keeper_score": (1, 5)}


def jpeg_1280(path):
    from PIL import Image
    im = Image.open(path)
    im.thumbnail((1280, 1280))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def validate(f):
    f = dict(f)
    for k, (lo, hi) in RANGES.items():
        if not isinstance(f.get(k), int) or isinstance(f.get(k), bool) or not lo <= f[k] <= hi:
            raise ValueError(f"bad {k}={f.get(k)!r}")
    t = f.get("horizon_tilt_deg")
    if t is not None and not isinstance(t, (int, float)):
        raise ValueError("bad horizon_tilt_deg")
    f["fixes"] = [x for x in f.get("fixes", []) if x in FIXES] or ["none"]
    return f


def review(path, key, url=API_URL, model=MODEL, retries=5, sleep=time.sleep):
    tool = {"name": "record_quality", "description": "Record the quality review for this photo.",
            "input_schema": SCHEMA, "strict": True}
    body = {"model": model, "max_tokens": 700, "tools": [tool], "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(jpeg_1280(path)).decode()}},
                {"type": "text", "text": PROMPT}]}]}
    last = "?"
    for i in range(retries):
        if i and last.startswith("no usable answer (stop_reason=max_tokens"):
            body["max_tokens"] = 1400  # retries only: first-pass settings stay identical to round 1
        try:
            r = _post(url, body, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=120)
        except urllib.error.HTTPError as e:
            last = f"http {e.code}: {scrub(e.read()[:300].decode('utf-8', 'replace'), key)}"
            if e.code not in (429, 500, 502, 503, 504, 529):
                raise RuntimeError(last) from None
        except (urllib.error.URLError, OSError) as e:
            last = "net: " + scrub(e, key)
        else:
            fields = None
            for b in r.get("content", []):
                if b.get("type") == "tool_use":
                    fields = b["input"]
                elif b.get("type") == "text" and fields is None:
                    try:
                        fields = parse_json(b["text"])
                    except ValueError:
                        pass
            if r.get("stop_reason") == "max_tokens" or fields is None:
                last = f"no usable answer (stop_reason={r.get('stop_reason')})"
            else:
                try:
                    return validate(fields), r.get("usage") or {}
                except ValueError as e:
                    last = str(e)
        sleep(min(2 ** i * 2, 30))
    raise RuntimeError(last)


def read_ndjson(p):
    out = []
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass  # partial last line
    return out


def run_reviews(ids, kinds, previews, out, key, max_usd, workers=3, log=print, extra=None, **kw):
    """Append one row per id to `out`; skip ids already there. Returns (done, spent, stopped_at_budget)."""
    have = read_ndjson(out)
    seen = {r["id"] for r in have}
    spent = sum(cost_usd(r.get("usage")) for r in have)
    todo = [i for i in ids if i not in seen]
    lock, st = threading.Lock(), {"spent": spent, "stop": False, "n": 0, "err": 0}

    def one(i):
        with lock:
            if st["spent"] >= max_usd:
                st["stop"] = True
                return
        try:
            f, u = review(os.path.join(previews, i + ".jpg"), key, **kw)
        except Exception as e:
            with lock:
                st["err"] += 1
            log(f"ERR {i}: {scrub(e, key)[:200]}")
            return
        row = {"id": i, "filename": kinds.get(i, {}).get("filename"), "kind": kinds.get(i, {}).get("kind", "arw"), **f,
               "usage": u, "model": kw.get("model", MODEL), **(extra or {})}
        with lock:
            st["spent"] += cost_usd(u)
            st["n"] += 1
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
                fh.flush()
            if st["n"] % 10 == 0:
                log(f"{st['n']}/{len(todo)} spent=${st['spent']:.3f}")

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, todo))
    if st["stop"]:
        log(f"stopped_at_budget spent=${st['spent']:.3f} cap=${max_usd}")
    return st["n"], st["spent"], st["stop"]


def kinds_map():
    return {r["id"]: {"filename": r["filename"], "kind": "hdr"} for r in csv.DictReader(open(HDR_CSV)) if r["variant"] == "1"}


def build_set(base=BASE, seed=20261031, hdr_csv=None):
    rnd = random.Random(seed)
    q = {r["id"]: r for r in read_ndjson(f"{base}/quality.ndjson") if not r.get("error")}
    scene = {r["photo_id"]: r["fields"]["scene_type"] for r in read_ndjson(f"{base}/final.ndjson")}
    km = kinds_map()
    info = {}
    for i, r in q.items():
        sev = {f["code"]: f.get("severity") for f in r.get("flags", [])}
        info[i] = dict(sev=sev, kind=km.get(i, {}).get("kind", "arw"), scene=scene.get(i, "other"))
    # priority: rarest strata first; a photo is in exactly one stratum (all its flags are still recorded)
    strata = [("dark", 60, {"underexposed", "crushed_shadows"}), ("tilted", 40, {"tilted_horizon"}),
              ("soft", 100, {"soft_focus"}), ("blown", 120, {"blown_highlights", "overexposed"}), ("noflag", 100, None)]
    chosen, out = set(), []
    for name, n, codes in strata:
        pool = sorted(i for i in info if i not in chosen and (not info[i]["sev"] if codes is None else codes & info[i]["sev"].keys()))
        rnd.shuffle(pool)
        buckets = collections.defaultdict(list)  # round-robin over kind x scene x severity for half, random for the rest
        for i in pool:
            sv = max((info[i]["sev"][c] or "" for c in (codes or ()) if c in info[i]["sev"]), default="")
            buckets[(info[i]["kind"], info[i]["scene"], sv)].append(i)
        pick = []
        while len(pick) < n // 2 and any(buckets.values()):
            for b in sorted(buckets):
                if buckets[b] and len(pick) < n // 2:
                    pick.append(buckets[b].pop())
        rest = [i for i in pool if i not in set(pick)]
        pick += rest[: n - len(pick)]
        for i in pick:
            chosen.add(i)
            out.append({"id": i, "stratum": name, "kind": info[i]["kind"], "filename": km.get(i, {}).get("filename"),
                        "scene_type": info[i]["scene"], "flags": info[i]["sev"]})
    return {"seed": seed, "n": len(out), "photos": out}


def agree(a_rows, b_rows):
    b = {r["id"]: r for r in b_rows}
    pairs = [(r, b[r["id"]]) for r in a_rows if r["id"] in b]
    res = {"n_pairs": len(pairs)}
    for k in ORD + ["exposure", "keeper_score"]:
        d = [abs(x[k] - y[k]) for x, y in pairs]
        res[k] = {"exact": round(sum(v == 0 for v in d) / max(len(d), 1), 3), "within1": round(sum(v <= 1 for v in d) / max(len(d), 1), 3)}
    d = [abs(x["horizon_tilt_deg"] - y["horizon_tilt_deg"]) for x, y in pairs if x["horizon_tilt_deg"] is not None and y["horizon_tilt_deg"] is not None]
    res["horizon_tilt_deg"] = {"n_both_non_null": len(d), "mean_abs_diff": round(sum(d) / len(d), 3) if d else None,
                               "null_agree": round(sum((x["horizon_tilt_deg"] is None) == (y["horizon_tilt_deg"] is None) for x, y in pairs) / max(len(pairs), 1), 3)}
    j = []
    for x, y in pairs:
        s, t = set(x["fixes"]) - {"none"}, set(y["fixes"]) - {"none"}
        j.append(1.0 if not (s | t) else len(s & t) / len(s | t))
    res["fixes_jaccard"] = round(sum(j) / max(len(j), 1), 3)
    return res


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("cmd", choices=["build-set", "review", "agree"])
    a.add_argument("--base", default=BASE)
    a.add_argument("--seed", type=int, default=20261031)
    a.add_argument("--max-usd", type=float, default=9.0)
    a.add_argument("--limit", type=int, default=0)
    a.add_argument("--all", action="store_true", help="round 2: label every preview not yet in labels.ndjson (rows get round=2)")
    a.add_argument("--new-usd", type=float, default=0, help="cap on NEW spend this run (added to spend already recorded in both label files)")
    a.add_argument("--repeat", type=int, default=0, help="re-review N random already-labelled photos into labels_repeat.ndjson")
    a.add_argument("--api-url", default=API_URL)
    o = a.parse_args(argv)
    rs, lab, rep = f"{o.base}/review_set.json", f"{o.base}/labels.ndjson", f"{o.base}/labels_repeat.ndjson"
    if o.cmd == "build-set":
        s = build_set(o.base, o.seed)
        json.dump(s, open(rs, "w"), indent=1)
        print(s["n"], collections.Counter(p["stratum"] for p in s["photos"]))
    elif o.cmd == "agree":
        print(json.dumps(agree(read_ndjson(lab), read_ndjson(rep)), indent=1))
    else:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            sys.exit("ANTHROPIC_API_KEY not in environment")
        km = kinds_map()
        if o.repeat:
            ids = [r["id"] for r in read_ndjson(lab)]
            ids = random.Random(o.seed).sample(ids, min(o.repeat, len(ids)))
            out = rep
        elif o.all:
            have = {r["id"] for r in read_ndjson(lab)}
            ids = sorted(f[:-4] for f in os.listdir(f"{o.base}/previews") if f.endswith(".jpg") and f[:-4] not in have)
            out = lab
        else:
            ids, out = [p["id"] for p in json.load(open(rs))["photos"]], lab
        if o.limit:
            ids = [i for i in ids if i not in {r["id"] for r in read_ndjson(out)}][: o.limit]
        # spend counts BOTH files so the cap is global
        other = sum(cost_usd(r.get("usage")) for r in read_ndjson(rep if out == lab else lab))
        cap = o.max_usd - other
        if o.new_usd:
            cap = sum(cost_usd(r.get("usage")) for r in read_ndjson(out)) + o.new_usd
        n, spent, stop = run_reviews(ids, km, f"{o.base}/previews", out, key, cap, extra={"round": 2} if o.all else None,
                                     log=lambda m: print(m, flush=True), url=o.api_url)
        print(f"done n={n} spent_this_file=${spent:.3f} stopped={stop}", flush=True)


if __name__ == "__main__":
    main()
