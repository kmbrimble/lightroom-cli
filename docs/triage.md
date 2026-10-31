# Triage pipeline: preview rendering and quality scoring

Two offline-friendly scripts feed a photo triage. Both are stdlib + `lightroom_sdk` / `lr_quality` only.

## 1. Render previews (`scripts/render_previews.py`)

Needs live Lightroom and the bridge. **The plugin serves one client at a time**: run nothing else against it meanwhile.
Read-only: it calls `catalog.getAllPhotos` once and `preview.generatePreview` per photo; it never writes to the catalog.

```
python scripts/render_previews.py [--out DIR] [--size 1600] [--limit N] [--ids-file ids.txt] [--retries 2] [--timeout 30]
```

- Default `--out` is `C:\NA2026\cl_scratch\previews`. One persistent connection, photos rendered serially.
- Resumable: an existing non-empty `<id>.jpg` is skipped. Files are written to `<id>.tmp` then renamed.
- Each photo is tried up to 1 + `--retries` times, reconnecting between tries. A failure is logged and never aborts the run.
- `--timeout` is the per-call seconds (default 30). The real run used `--timeout 10`: a normal render takes 0.2-1.5 s, but about 1 call in 10 never gets a reply; a short timeout plus reconnect-and-retry recovers it cheaply.
- Exit code 1 if any photo failed. Prints a JSON summary `{total, rendered, skipped, failed, median_secs}`.
- To survive a dying ssh session on the desktop, run it as an interactive scheduled task:
  `schtasks /create /tn lrcli-render /tr "<venv python> <script>" /sc once /st 23:59 /it /f` then `schtasks /run /tn lrcli-render`,
  and `schtasks /delete /tn lrcli-render /f` afterwards. (Do not put `>` redirects in `/tr`; there is no shell.)

Outputs in `--out`:

| File | Content |
|---|---|
| `<id>.jpg` | the 1600 px preview (id = Lightroom local id) |
| `render.ndjson` | one line per attempted photo: `{id, filename, ok, bytes, secs, attempts, error, attempt_errors}` (`secs` covers all attempts) |
| `photos.json` | list from `catalog.getAllPhotos`: `{id, filename, path, fileFormat, captureTime, pickStatus}` |

## 2. Score previews (`scripts/score_previews.py`)

Pure offline (numpy + Pillow via `lr_quality`); no Lightroom.

```
python scripts/score_previews.py [--previews DIR] [--out FILE] [--workers 8] [--limit N]
```

- Defaults: `C:\NA2026\cl_scratch\previews` -> `C:\NA2026\cl_scratch\quality.ndjson`. Multiprocessing, up to 8 workers.
- Scene hint is `landscape` unless the original filename (from `photos.json`) contains another scene word (`city, people, sunset, snow, interior, night`).
- Resumable: per-image raw results are cached in `<out>.raw` (includes errors); only images without a cached success are analysed.
  Batch-relative fields and flags are always recomputed over the whole set and `<out>` is replaced atomically.
- Output: one line per scored image, `{"id": "<id>", ...}` followed by the full `lr_quality/2` object (see `docs/quality.md`),
  with `exposure.batch_percentile` and `focus.relative_to_batch` relative to the whole folder. Images that fail to decode appear only in `.raw`.
- Exit code 1 if any image failed.

## Timings

Filled in from the real run (2026-10-31, Windows desktop, Lightroom Classic 15.6): see the final section below.
