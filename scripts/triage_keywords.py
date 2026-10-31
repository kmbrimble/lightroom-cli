#!/usr/bin/env python3
"""Build the keyword plan from final.ndjson: writes plan.ndjson and prints per-keyword counts (offline, no Lightroom)."""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lr_triage import keywords as K  # noqa: E402

a = argparse.ArgumentParser()
a.add_argument("--in", dest="inp", default="/projects/.lr-scratch/triage/final.ndjson")
a.add_argument("--out", default="/projects/.lr-scratch/triage/keyword_plan.ndjson")
o = a.parse_args()
plan, dropped = K.plan(K.load(o.inp))
with open(o.out, "w", encoding="utf-8") as f:
    f.writelines(json.dumps(p) + "\n" for p in plan)
c = collections.Counter(k for p in plan for k in p["keywords"])
print(f"{len(plan)} photos, {len(c)} distinct keywords -> {o.out}\ndropped: {dict(dropped)}")
print(", ".join(f"{k} ({v})" for k, v in c.most_common(40)))
