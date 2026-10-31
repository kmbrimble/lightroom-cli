#!/usr/bin/env python3
"""Build review_plan.ndjson from quality-labels/labels.ndjson (+ keyword_plan.ndjson for canyon). Dry run: counts + first 10 ids."""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lr_triage import review_keywords as RK  # noqa: E402

B = "/projects/.lr-scratch"
plan = RK.plan(RK.load(B + "/quality-labels/labels.ndjson"), RK.load(B + "/triage/keyword_plan.ndjson"))
out = sys.argv[1] if len(sys.argv) > 1 else B + "/triage/review_plan.ndjson"
with open(out, "w", encoding="utf-8") as f:
    f.writelines(json.dumps(p) + "\n" for p in plan)
c = collections.Counter(k for p in plan for k in p["keywords"])
print(f"{len(plan)} photos get at least one keyword; {len(c)} keywords -> {out}")
for k in RK.NAMES:
    ids = [p["photo_id"] for p in plan if k in p["keywords"]]
    print(f"{c[k]:5d}  {k}  first10={ids[:10]}")
