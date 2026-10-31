#!/usr/bin/env python3
"""Container-side escalation: triage.ndjson (desktop local pass) -> Claude for escalated ids -> final.ndjson.

  hl exec --secret ANTHROPIC_API_KEY=claude-code/anthropic_api_key -- python3 scripts/triage_escalate.py --pull --limit 5
The key exists only in this process's env: never printed, written, or put on argv. No key or --dry-run-escalation: records only.
"""
import argparse, collections, json, os, subprocess, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lr_triage.cascade import Escalator, escalation_reasons, load_quality  # noqa: E402

REMOTE = "9950x:C:/NA2026/cl_scratch/"


def scp(src, dst):
    return subprocess.run(["scp", "-O", "-q", src, dst], capture_output=True, timeout=120).returncode == 0


def main(argv=None):
    a = argparse.ArgumentParser()
    base = "/projects/.lr-scratch/triage"
    a.add_argument("--triage", default=base + "/triage.ndjson")
    a.add_argument("--previews", default=base + "/previews")
    a.add_argument("--out", default=base + "/final.ndjson")
    a.add_argument("--pull", action="store_true", help="scp -O triage.ndjson and the escalated previews from the desktop")
    a.add_argument("--quality", default=base + "/quality.ndjson")
    a.add_argument("--max-usd", type=float, default=8.0)
    a.add_argument("--dry-run-escalation", action="store_true")
    a.add_argument("--limit", type=int, default=0, help="escalate at most N photos this run (testing)")
    a.add_argument("--force", type=int, default=0, help="test: also escalate the N lowest-min_conf records (reason forced_test)")
    a.add_argument("--api-url", default=None)
    o = a.parse_args(argv)
    os.makedirs(o.previews, exist_ok=True)
    key = os.environ.get("ANTHROPIC_API_KEY") or None
    if o.pull and not scp(REMOTE + "triage.ndjson", o.triage):
        sys.exit("could not pull triage.ndjson")
    if o.pull:
        scp(REMOTE + "quality.ndjson", o.quality)  # may not exist yet (A1 may write it at the end)
    quality = load_quality(o.quality)
    done, spent = set(), 0.0
    if os.path.exists(o.out):
        for l in open(o.out, encoding="utf-8"):
            r = json.loads(l)
            done.add(r["photo_id"])
            spent += (r.get("claude") or {}).get("cost_usd", 0.0)
    esc = Escalator(key, o.max_usd, o.dry_run_escalation, spent, **({"url": o.api_url} if o.api_url else {}))
    print("escalation:", "DRY RUN" if esc.dry else f"live, max ${o.max_usd}, already spent ${spent:.4f}", flush=True)
    sent = 0
    lines = [l for l in open(o.triage, encoding="utf-8") if l.strip()]
    forced = set()
    if o.force:
        low = sorted((json.loads(l) for l in lines), key=lambda r: r["min_conf"] if r["min_conf"] is not None else -1)
        forced = {r["photo_id"] for r in low[:o.force]}
    for line in lines:
        rec = json.loads(line)
        if rec["photo_id"] in forced and not rec["escalate"]:
            rec["escalate"], rec["reasons"] = True, ["forced_test"]
        pid = rec["photo_id"]
        if pid in done:
            continue
        if pid in quality:  # always re-decide here with the current rules + quality (desktop reasons are advisory)
            rec["reasons"] = escalation_reasons(rec["local"], quality[pid])
            rec["escalate"], rec["quality_missing"] = bool(rec["reasons"]), False
        elif not rec["escalate"] and pid in forced:
            rec["escalate"], rec["reasons"] = True, ["forced_test"]
        img = f"{o.previews}/{pid}.jpg"
        if rec["escalate"] and not esc.dry and esc.spent < o.max_usd:
            if o.limit and sent >= o.limit:
                continue  # leave for a later run
            if not os.path.exists(img) and not (o.pull and scp(f"{REMOTE}previews/{pid}.jpg", img)):
                print(f"{pid}: preview missing, skipped", file=sys.stderr)
                continue
            sent += 1
        out = esc.run(rec, img)
        if out.get("escalation", "").startswith("error"):  # transient: not written, so a rerun retries it
            print(f"{pid}: {out['escalation'][:160]}", file=sys.stderr, flush=True)
            continue
        with open(o.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(out, ensure_ascii=False) + "\n")
        if out.get("claude"):
            u = out["claude"]["usage"]
            print(f"{pid} {rec['fields']['scene_type']}->{out['fields']['scene_type']} in={u.get('input_tokens')} "
                  f"out={u.get('output_tokens')} ${out['claude']['cost_usd']:.4f} total ${esc.spent:.4f}", flush=True)
    rs = [json.loads(l) for l in open(o.out, encoding="utf-8")] if os.path.exists(o.out) else []
    cl = [r for r in rs if r.get("claude")]
    print(f"final: {len(rs)} records, {len(cl)} from Claude, spent ${esc.spent:.4f}"
          + (f", ${esc.spent / len(cl):.4f}/image" if cl else "")
          + f"; escalation status {dict(collections.Counter(r.get('escalation', '-') for r in rs if r['escalate']))}")


if __name__ == "__main__":
    main()
