"""lr-quality CLI: analyze | batch | learn-targets. stdout is JSON/NDJSON only; exit 0 ok, 1 some failed, 2 usage."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import core

EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}


def _out(obj, fh=None):
    (fh or sys.stdout.buffer).write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))


def _json_file(p):
    return json.loads(Path(p).read_text("utf-8")) if p else None


def _expand(paths):
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            yield from (str(x) for x in sorted(pp.rglob("*")) if x.suffix.lower() in EXTS)
        else:
            yield str(p)


def _kw(a):
    return dict(scene_hint=a.scene_hint, thresholds=_json_file(a.thresholds), targets=_json_file(a.targets),
                long_edge=a.long_edge, accel="numpy" if a.no_cv2 else "auto")


def _err(src, e):
    return {"schema": core.SCHEMA, "source": src, "error": f"{type(e).__name__}: {e}"}


def cmd_analyze(a):
    src = None if a.image == "-" else a.image
    try:
        data = sys.stdin.buffer.read() if src is None else src
        r = core.analyze(data, **_kw(a))
        r["source"] = src
    except Exception as e:
        _out(_err(src, e))
        return 1
    _out(r)
    return 0


def cmd_batch(a):
    paths = list(a.paths)
    if a.list:
        paths += [x.strip() for x in Path(a.list).read_text("utf-8").splitlines() if x.strip()]
    if a.stdin_list:
        paths += [x.strip() for x in sys.stdin.read().splitlines() if x.strip()]
    paths = list(_expand(paths))
    if not paths:
        print("lr-quality batch: no input paths", file=sys.stderr)
        return 2
    kw = _kw(a)
    results = []
    for p in paths:
        try:
            r = core.analyze_raw(p, **kw)
            r["source"] = p
        except Exception as e:
            r = _err(p, e)
        results.append(r)
    core.batch_pass(results)
    fh = open(a.out, "wb") if a.out else None
    try:
        for r in results:
            _out(r if "error" in r else core.finalize(r), fh)
    finally:
        if fh:
            fh.close()
    return 1 if any("error" in r for r in results) else 0


def cmd_learn(a):
    root = Path(a.folder)
    if not root.is_dir():
        print(f"lr-quality learn-targets: not a folder: {a.folder}", file=sys.stderr)
        return 2
    by = {}
    failed = 0
    for p in sorted(x for x in root.rglob("*") if x.suffix.lower() in EXTS):
        rel = p.relative_to(root).parts
        cls = rel[0] if len(rel) > 1 else "default"
        try:
            by.setdefault(cls, []).append(core.analyze(str(p), long_edge=a.long_edge)["exposure"])
        except Exception as e:
            failed += 1
            print(f"skip {p}: {e}", file=sys.stderr)
    if not by:
        print("lr-quality learn-targets: no readable images", file=sys.stderr)
        return 1
    if "default" not in by:
        by["default"] = [e for v in by.values() for e in v]
    keys = ["mean_luma"] + [f"p{p:02d}" for p in core._PCTS]
    scenes = {c: {"n": len(v), **{k: round(float(np.median([e[k] for e in v])), 3) for k in keys}} for c, v in by.items()}
    doc = {"schema": "lr_quality_targets/1", "scenes": scenes}
    Path(a.out).write_text(json.dumps(doc, indent=2) + "\n", "utf-8")
    _out({"written": a.out, "scenes": {c: s["n"] for c, s in scenes.items()}, "failed": failed})
    return 1 if failed else 0


def _common(sp, targets=True):
    sp.add_argument("--scene-hint", choices=core.SCENES)
    sp.add_argument("--thresholds", metavar="FILE.json")
    sp.add_argument("--targets", metavar="targets.json")
    sp.add_argument("--long-edge", type=int, default=1600)
    sp.add_argument("--no-cv2", action="store_true", help="force the pure numpy path")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lr-quality", description="Exposure / focus / tilt / noise metrics as JSON.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("analyze", help="one image (path, or - for bytes on stdin) -> one JSON object")
    s.add_argument("image")
    _common(s)
    s.set_defaults(fn=cmd_analyze)
    s = sub.add_parser("batch", help="many images -> NDJSON with batch-relative fields")
    s.add_argument("paths", nargs="*", help="files or folders")
    s.add_argument("--list", metavar="FILE")
    s.add_argument("--stdin-list", action="store_true")
    s.add_argument("--out", metavar="FILE.ndjson")
    _common(s)
    s.set_defaults(fn=cmd_batch)
    s = sub.add_parser("learn-targets", help="median percentiles from your best edits (subfolder = scene class)")
    s.add_argument("folder")
    s.add_argument("--out", default="targets.json")
    s.add_argument("--long-edge", type=int, default=1600)
    s.set_defaults(fn=cmd_learn)
    try:
        a = ap.parse_args(argv)
        return a.fn(a)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    except (OSError, ValueError, json.JSONDecodeError) as e:  # bad --thresholds etc.
        print(f"lr-quality: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
