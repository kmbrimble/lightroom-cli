#!/usr/bin/env python3
"""Resumable triage driver: local VLM pass + escalation cascade over a previews folder.

  python triage_classify.py --previews C:\\NA2026\\cl_scratch\\previews --watch
Local pass only: records `escalate` + `reasons`; it never reads an API key. Escalation to Claude is scripts/triage_escalate.py (container).
"""
import argparse, collections, json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lr_triage.cascade import EST_IN, EST_OUT, PRICE_IN, PRICE_OUT, decide, load_quality  # noqa: E402
from lr_triage.classify import local_classify  # noqa: E402


def read_done(path):
    done = set()
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            done.add(r["photo_id"])
    return done


def report(path, total=1523):
    rs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    n = len(rs) or 1
    esc = [r for r in rs if r["escalate"]]
    reasons = collections.Counter(x.split(":")[0] + (":" + x.split(":")[1] if x.startswith(("low_conf", "quality_")) else "")
                                  for r in esc for x in r["reasons"])
    done = [r for r in rs if r.get("claude")]
    per = (sum(r["claude"]["cost_usd"] for r in done) / len(done)) if done else (EST_IN * PRICE_IN + EST_OUT * PRICE_OUT) / 1e6
    mc = sorted(r["min_conf"] for r in rs if r["min_conf"] is not None)
    L = [f"# Triage report ({len(rs)} photos)", "", "## Scene types"]
    L += [f"- {k}: {v}" for k, v in collections.Counter(r["fields"].get("scene_type") for r in rs).most_common()]
    L += ["", f"## Escalation: {len(esc)}/{len(rs)} = {100 * len(esc) / n:.1f}%",
          f"(sent to Claude, from final.ndjson if used: {len(done)}; budget_exhausted: {sum(r.get('escalation') == 'budget_exhausted' for r in rs)};"
          f" errors: {sum(str(r.get('escalation', '')).startswith('error') for r in rs)})"]
    L += [f"- {k}: {v}" for k, v in reasons.most_common()]
    L += ["", f"min_conf median {mc[len(mc) // 2]:.3f}, p10 {mc[len(mc) // 10]:.3f}" if mc else "no confidences",
          f"## Projected Sonnet cost for {total} photos: ${per * total * len(esc) / n:.2f} "
          f"(${per:.4f}/escalated photo, {'measured' if done else 'ESTIMATED from token counts'})"]
    return "\n".join(L)


def main(argv=None):
    a = argparse.ArgumentParser()
    d = r"C:\NA2026\cl_scratch"
    a.add_argument("--previews", default=d + r"\previews")
    a.add_argument("--quality", default=d + r"\quality.ndjson")
    a.add_argument("--out", default=d + r"\triage.ndjson")
    a.add_argument("--url", default="http://127.0.0.1:8081")
    a.add_argument("--watch", action="store_true", help="keep waiting for new previews until render.done or --until-count")
    a.add_argument("--until-count", type=int, default=0)
    a.add_argument("--limit", type=int, default=0, help="stop after N new photos (testing)")
    a.add_argument("--quality-grace", type=float, default=300, help="seconds to wait for a photo's lr_quality record")
    a.add_argument("--poll", type=float, default=5)
    a.add_argument("--max-minutes", type=float, default=0)
    a.add_argument("--report", default=None)
    o = a.parse_args(argv)
    done = read_done(o.out)
    t0, new, quality, qload = time.time(), 0, {}, 0
    first_seen = {}
    while True:
        if time.time() - qload > 10:
            quality, qload = load_quality(o.quality), time.time()
        files = sorted((f for f in os.listdir(o.previews) if f.lower().endswith(".jpg")),
                       key=lambda f: os.path.getmtime(os.path.join(o.previews, f))) if os.path.isdir(o.previews) else []
        finished = os.path.exists(os.path.join(o.previews, "render.done"))
        todo = []
        for f in files:
            pid, p = f[:-4], os.path.join(o.previews, f)
            if pid in done or time.time() - os.path.getmtime(p) < 2:  # still being written
                continue
            first_seen.setdefault(pid, time.time())
            if pid not in quality and not finished and time.time() - first_seen[pid] < o.quality_grace and o.watch:
                continue  # wait for lr_quality, but never block the whole queue on it
            todo.append((pid, p))
        for pid, p in todo[:1]:
            try:
                loc = local_classify(p, o.url)
            except Exception as e:
                print(f"{pid}: {e}", file=sys.stderr, flush=True)
                done.add(pid)  # don't loop forever on a bad file; rerun with a new --out to retry
                continue
            rec = decide(pid, loc, quality)
            rec["quality_missing"] = pid not in quality
            with open(o.out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done.add(pid)
            new += 1
            print(f"{pid} {rec['fields']['scene_type']} min_conf={rec['min_conf']} esc={rec['reasons']}", flush=True)
        if todo:
            if o.limit and new >= o.limit:
                break
            continue
        if not o.watch or finished and not [f for f in files if f[:-4] not in done] or (o.until_count and len(done) >= o.until_count):
            break
        if o.max_minutes and time.time() - t0 > o.max_minutes * 60:
            print("max-minutes reached", flush=True)
            break
        time.sleep(o.poll)
    if os.path.exists(o.out):
        text = report(o.out)
        print(text)
        open(o.report or o.out.rsplit(".", 1)[0] + "_report.md", "w", encoding="utf-8").write(text)


if __name__ == "__main__":
    main()
