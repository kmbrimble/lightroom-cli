"""Score a folder of <id>.jpg previews with lr_quality (offline, no Lightroom).

Per-image raw results are cached in <out>.raw (resumable); at the end batch-relative fields
(focus.relative_to_batch, exposure.batch_percentile) and flags are computed over the WHOLE set
and written atomically to <out> as NDJSON: {"id": N, **lr_quality result}.
Scene hint is 'landscape' unless the original filename (from photos.json, if present) or the
preview name contains another scene word (city, people, sunset, snow, interior, night).

usage: score_previews.py [--previews DIR] [--out FILE] [--workers 8] [--limit N]
"""
import argparse
import json
import os
import re
import sys
from multiprocessing import Pool
from pathlib import Path

from lr_quality import core

PREVIEWS = r"C:\NA2026\cl_scratch\previews"
OUT = r"C:\NA2026\cl_scratch\quality.ndjson"


def hint_for(*names):
    text = " ".join(n for n in names if n).lower()
    for s in core.SCENES:
        if re.search(rf"(?<![a-z]){s}(?![a-z])", text):
            return s
    return "landscape"


def _work(job):
    pid, path, hint = job
    try:
        r = core.analyze_raw(path, scene_hint=hint)
        r["source"] = path
        r["id"] = pid
        return r
    except Exception as e:
        return {"id": pid, "source": path, "error": f"{type(e).__name__}: {e}"}


def run(previews, out, workers=8, limit=None, log=print):
    previews, out = Path(previews), Path(out)
    raw_path = out.with_suffix(out.suffix + ".raw")
    names = {}
    if (previews / "photos.json").exists():
        names = {str(p["id"]): p.get("filename") for p in json.loads((previews / "photos.json").read_text("utf-8"))}
    files = sorted(previews.glob("*.jpg"), key=lambda p: (len(p.stem), p.stem))
    if limit:
        files = files[:limit]
    ids = {p.stem for p in files}
    done = {}
    if raw_path.exists():
        for line in raw_path.read_text("utf-8").splitlines():
            r = json.loads(line)
            if "error" not in r:
                done[str(r["id"])] = r
    jobs = [(p.stem, str(p), hint_for(names.get(p.stem), p.stem)) for p in files if p.stem not in done]
    with open(raw_path, "a", encoding="utf-8") as raw, Pool(max(1, min(workers, os.cpu_count() or 1))) as pool:
        for r in pool.imap_unordered(_work, jobs, chunksize=4):
            raw.write(json.dumps(r) + "\n")
            raw.flush()
            if "error" not in r:
                done[str(r["id"])] = r
    results = [done[i] for i in sorted(done, key=lambda s: (len(s), s)) if i in ids]
    core.batch_pass(results)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({"id": r["id"], **core.finalize(r)}) + "\n")
    os.replace(tmp, out)
    summary = {"images": len(files), "scored": len(results), "failed": len(files) - len(results), "out": str(out)}
    log(json.dumps(summary))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--previews", default=PREVIEWS)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    return 1 if run(a.previews, a.out, a.workers, a.limit)["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
