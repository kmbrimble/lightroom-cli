"""Render a 1600px Lightroom preview JPEG for every photo in the catalog (read-only).

One persistent bridge connection, serial. Resumable (skips non-empty <id>.jpg).
Logs NDJSON {id, filename, ok, bytes, secs, attempts, error, attempt_errors} to <out>/render.ndjson.

usage: render_previews.py [--out DIR] [--size 1600] [--limit N] [--ids-file F] [--retries 2]
The plugin serves ONE client at a time: do not run anything else against the bridge meanwhile.
"""
import argparse
import asyncio
import base64
import json
import os
import statistics
import sys
import time
from pathlib import Path

DEFAULT_OUT = r"C:\NA2026\cl_scratch\previews"


async def list_photos(client, page=500):
    photos, offset = [], 0
    while True:
        r = await client.execute_command("catalog.getAllPhotos", {"limit": page, "offset": offset}, timeout=120)
        batch = r.get("photos", [])
        photos += batch
        offset += len(batch)
        if not batch or offset >= r.get("total", 0):
            return photos


async def _render_one(client, pid, size, timeout):
    r = await client.execute_command("preview.generatePreview", {"photoId": pid, "size": size}, timeout=timeout)
    b64 = r.get("preview")
    if not b64:
        raise RuntimeError(f"no preview in result (keys: {sorted(r)})")
    data = base64.b64decode(b64)
    if not data:
        raise RuntimeError("empty preview")
    return data


async def run(make_client, out, size=1600, limit=None, ids=None, retries=2, timeout=30, log=print):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    client = make_client()
    await client.connect()
    summary = {"total": 0, "rendered": 0, "skipped": 0, "failed": 0, "secs": []}
    try:
        photos = await list_photos(client)
        (out / "photos.json").write_text(json.dumps(photos), "utf-8")
        if ids is not None:
            photos = [p for p in photos if str(p["id"]) in ids]
        if limit:
            photos = photos[:limit]
        summary["total"] = len(photos)
        with open(out / "render.ndjson", "a", encoding="utf-8") as ndj:
            for p in photos:
                pid, dest = p["id"], out / f"{p['id']}.jpg"
                if dest.exists() and dest.stat().st_size > 0:
                    summary["skipped"] += 1
                    continue
                rec = {"id": pid, "filename": p.get("filename"), "ok": False, "bytes": 0, "secs": 0.0, "attempts": 0, "error": None, "attempt_errors": []}
                t0 = time.time()
                for attempt in range(retries + 1):
                    rec["attempts"] = attempt + 1
                    try:
                        data = await _render_one(client, pid, size, timeout)
                        tmp = dest.with_suffix(".tmp")
                        tmp.write_bytes(data)
                        os.replace(tmp, dest)
                        rec.update(ok=True, bytes=len(data), error=None)
                        break
                    except Exception as e:  # never raise past one photo
                        rec["error"] = f"{type(e).__name__}: {e}"
                        rec["attempt_errors"].append(f"{rec['error']} after {time.time() - t0:.1f}s")
                        if attempt < retries:
                            try:  # reconnect (a half-dead socket is the usual cause)
                                await client.disconnect()
                            except Exception:
                                pass
                            client = make_client()
                            try:
                                await client.connect()
                            except Exception as ce:
                                rec["error"] += f" (reconnect failed: {ce})"
                rec["secs"] = round(time.time() - t0, 2)
                summary["rendered" if rec["ok"] else "failed"] += 1
                if rec["ok"]:
                    summary["secs"].append(rec["secs"])
                ndj.write(json.dumps(rec) + "\n")
                ndj.flush()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    s = summary.pop("secs")
    summary["median_secs"] = round(statistics.median(s), 2) if s else None
    log(json.dumps(summary))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--size", type=int, default=1600)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids-file", help="file with one photo id per line")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=30, help="per-call seconds (normal render is <2 s)")
    a = ap.parse_args(argv)
    ids = None
    if a.ids_file:
        ids = {x.strip() for x in Path(a.ids_file).read_text().splitlines() if x.strip()}
    from lightroom_sdk.client import LightroomClient

    res = asyncio.run(run(LightroomClient, a.out, a.size, a.limit, ids, a.retries, a.timeout))
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
