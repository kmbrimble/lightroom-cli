#!/usr/bin/env python3
"""Summary stats over final.ndjson (or triage.ndjson) + quality.ndjson. Re-evaluates escalation reasons with the current rules."""
import argparse, collections, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lr_triage.cascade import LOCAL_TO_Q, escalation_reasons, load_quality  # noqa: E402


def main():
    a = argparse.ArgumentParser()
    base = "/projects/.lr-scratch/triage"
    a.add_argument("--in", dest="inp", default=base + "/final.ndjson")
    a.add_argument("--quality", default=base + "/quality.ndjson")
    o = a.parse_args()
    q = load_quality(o.quality)
    rs = [json.loads(l) for l in open(o.inp, encoding="utf-8") if l.strip()]
    n = len(rs)
    P = lambda *x: print(*x)  # noqa: E731
    P(f"# Summary of {o.inp}: {n} photos\n\n## Scene types (final answer)")
    for k, v in collections.Counter(r["fields"]["scene_type"] for r in rs).most_common():
        P(f"- {k}: {v}")
    cnt = [r["fields"]["people_count"] for r in rs]
    with_p = [c for c in cnt if c > 0]
    P(f"\n## People\n- has_people: {len(with_p)}/{n}; total people {sum(cnt)}; mean {sum(with_p) / max(1, len(with_p)):.2f} per photo with people; "
      f"max {max(cnt or [0])}; close-ups {sum(1 for r in rs if r['fields']['people_count'] and r['fields']['is_close_up_person'])}")
    P("- count histogram: " + ", ".join(f"{k}:{v}" for k, v in sorted(collections.Counter(min(c, 6) for c in cnt).items())) + " (6 = 6+)")
    P("\n## Problems: local (local pass) vs lr_quality (warn/bad flags)\n| problem | both | local only | quality only | neither |\n|---|---|---|---|---|")
    for pb, codes in LOCAL_TO_Q.items():
        b = lo = qo = ne = 0
        for r in rs:
            lp = pb in r["local"]["fields"]["visible_problems"]
            qf = {x["code"] for x in q.get(r["photo_id"], {}).get("flags", []) if x["severity"] in ("warn", "bad")}
            qp = bool(codes & qf)
            b += lp and qp; lo += lp and not qp; qo += qp and not lp; ne += not lp and not qp
        P(f"| {pb} | {b} | {lo} | {qo} | {ne} |")
    P(f"- local says 'none': {sum(r['local']['fields']['visible_problems'] == ['none'] for r in rs)}/{n}")
    reasons = collections.Counter()
    esc = 0
    for r in rs:
        w = escalation_reasons(r["local"], q.get(r["photo_id"]))
        esc += bool(w)
        reasons.update(w)
    P(f"\n## Escalation under current rules: {esc}/{n} = {100 * esc / max(n, 1):.1f}% (photos can carry several reasons)")
    for k, v in reasons.most_common():
        P(f"- {k}: {v} ({100 * v / n:.1f}% of photos)")
    cl = [r for r in rs if r.get("claude")]
    if cl:
        usd = sum(r["claude"]["cost_usd"] for r in cl)
        tin = sum(r["claude"]["usage"]["input_tokens"] for r in cl)
        tout = sum(r["claude"]["usage"]["output_tokens"] for r in cl)
        agree = sum(r["local"]["fields"]["scene_type"] == r["claude"]["fields"]["scene_type"] for r in cl)
        P(f"\n## Claude pass\n- {len(cl)} photos, {tin} in / {tout} out tokens, ${usd:.4f} total, ${usd / len(cl):.4f}/photo; "
          f"scene_type agrees with local on {agree}/{len(cl)}")
        for f in ("has_people", "is_close_up_person"):
            P(f"- {f} agrees: {sum(r['local']['fields'][f] == r['claude']['fields'][f] for r in cl)}/{len(cl)}")
        P(f"- visible_problems identical: {sum(sorted(r['local']['fields']['visible_problems']) == sorted(r['claude']['fields']['visible_problems']) for r in cl)}/{len(cl)}")
    kw = collections.Counter(k.lower().strip() for r in rs for k in r["fields"]["keywords"])
    P(f"\n## Top keywords ({len(kw)} distinct)\n" + ", ".join(f"{k} ({v})" for k, v in kw.most_common(40)))


if __name__ == "__main__":
    main()
