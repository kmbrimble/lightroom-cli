#!/usr/bin/env python3
"""Whole-catalog summary of labels.ndjson -> review_summary.md + review_candidates.csv (rejects: keeper_score <= 2)."""
import collections, csv, json, os, sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "/projects/.lr-scratch/quality-labels"
rows = {}
for line in open(f"{BASE}/labels.ndjson", encoding="utf-8"):
    try:
        r = json.loads(line)
        rows[r["id"]] = r
    except ValueError:
        pass
L = list(rows.values())
# a fix is listed when the photo carries it AND the matching severity >= 2 (or the photo is a reject, keeper_score <= 2)
sev = {"lower_highlights": lambda r: r["highlights"] >= 2, "lift_shadows": lambda r: r["shadows"] >= 2,
       "raise_exposure": lambda r: r["exposure"] <= -2, "lower_exposure": lambda r: r["exposure"] >= 2,
       "straighten": lambda r: abs(r["horizon_tilt_deg"] or 0) >= 1, "sharpen": lambda r: r["focus"] >= 2 or r["motion_blur"] >= 2}
out = [f"# Quality label summary\n\n{len(L)} photos labelled ({sum(r.get('round') == 2 for r in L)} in round 2, "
       f"{sum(r['kind'] == 'hdr' for r in L)} hdr / {sum(r['kind'] == 'arw' for r in L)} arw). Rubric: lr_quality/rubric.md.\n",
       "## Counts per field\n", "| field | " + " | ".join(map(str, range(-3, 6))) + " |", "|---|" + "---|" * 9]
for k in ["highlights", "shadows", "exposure", "focus", "motion_blur", "noise", "keeper_score"]:
    c = collections.Counter(r[k] for r in L)
    out.append(f"| {k} | " + " | ".join(str(c.get(v, 0)) or "0" for v in range(-3, 6)) + " |")
out.append(f"\nhorizon_tilt_deg: null {sum(r['horizon_tilt_deg'] is None for r in L)}, |tilt|>=1 {sum(abs(r['horizon_tilt_deg'] or 0) >= 1 for r in L)}, "
           f">=2 {sum(abs(r['horizon_tilt_deg'] or 0) >= 2 for r in L)}. uncertain: {sum(r['uncertain'] for r in L)}.\n")
out.append("fixes (all): " + ", ".join(f"{k} {v}" for k, v in collections.Counter(f for r in L for f in r["fixes"]).most_common()) + "\n")
out.append("## Photo ids per fix (fix present and severity >= 2 [straighten: |tilt| >= 1, since no label reaches 2], or keeper_score <= 2)\n")
for f, fn in sev.items():
    ids = sorted(r["id"] for r in L if f in r["fixes"] and (fn(r) or r["keeper_score"] <= 2))
    out.append(f"### {f} ({len(ids)})\n\n" + ", ".join(ids) + "\n")
rej = sorted((r for r in L if r["keeper_score"] <= 2), key=lambda r: (r["keeper_score"], r["id"]))
with open(f"{BASE}/review_candidates.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["id", "filename", "keeper_score", "fixes", "note"])
    for r in rej:
        w.writerow([r["id"], r.get("filename") or "", r["keeper_score"], " ".join(r["fixes"]), r["note"]])
out.append(f"## Rejects (keeper_score <= 2): {len(rej)}\n\nSee review_candidates.csv.\n")
open(f"{BASE}/review_summary.md", "w", encoding="utf-8").write("\n".join(out))
print(len(L), len(rej))
