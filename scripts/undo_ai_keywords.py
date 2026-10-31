#!/usr/bin/env python3
"""Undo triage_apply.py: remove exactly the keywords it added (log-driven) via catalog.removeKeyword. Run ON the desktop.

    python undo_ai_keywords.py --log run.ndjson [run_review.ndjson ...] [--dry-run] [--lrc PATH] [--undo-log undo.ndjson]

Removes only names in each photo's 'to_add' (keywords the photo did not already have). Resumable (undo log), sequential,
KEYWORD_NOT_FOUND counts as done. The keyword objects stay in the catalog keyword list with 0 photos (delete them in the
Keyword List panel if wanted); smart collections 'AI - *' are also removed by hand. Never touches anything else.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage_apply as T  # noqa: E402


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("--log", required=True, nargs="+", help="one or more apply logs (triage run.ndjson, run_review.ndjson)")
    a.add_argument("--undo-log", default="undo_ai_keywords_log.ndjson")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--lrc")
    o = a.parse_args(argv)
    todo = {}
    for lg in o.log:
        for e in T.read_ndjson(lg):
            if e["status"] == "start":
                todo.setdefault(e["photo_id"], set()).update(e["to_add"])
    done = {(e["photo_id"], e["keyword"]) for e in T.read_ndjson(o.undo_log) if e["status"] == "ok"}
    jobs = [(p, k) for p, ks in todo.items() for k in sorted(ks) if (p, k) not in done]
    print(f"{len(jobs)} removals ({len(done)} already undone)")
    if o.dry_run:
        return 0
    d = T.Daemon(T.load_lrc(o.lrc))
    if not d.healthy():
        print("daemon not healthy"); return 3
    bad = 0
    for p, k in jobs:
        r = d.call("catalog.removeKeyword", {"photoId": int(p), "keyword": k})
        code = (r.get("error") or {}).get("code")
        if r.get("ok") or code == "KEYWORD_NOT_FOUND":
            T.append(o.undo_log, {"photo_id": p, "keyword": k, "status": "ok"})
        else:
            bad += 1
            T.append(o.undo_log, {"photo_id": p, "keyword": k, "status": "error", "error": r.get("error")})
            if code in ("BRIDGE_DOWN", "DAEMON_DOWN", "GUARD_DENIED") or bad >= 5:
                print("ABORT", code); return 2
    print(f"done, {bad} errors")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
