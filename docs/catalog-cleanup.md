# Catalog clean-up runner (`scripts/catalog_cleanup.py`)

One-off, resumable. Photos leave the **catalog only**; the script has no code path that touches files on disk
(it sends five bridge commands, none of which delete files, and writes one append-only log).

| Step | Action | Count |
|---|---|---|
| A | remove the `-HDR-2.dng` merge of each duplicate group (the `-HDR.dng` partner stays) | 26 |
| B | remove the source ARWs of every bracket set that still has a merge | **4,458** (949 sets) |
| C | `develop.clearAutoTone` on every photo left | 1,523 |

Kept: 84 ARWs of the 23 merge-less sets, the 3 focus-bracket frames and the 487 plain single ARWs.
Catalog total: 6,007 -> 5,981 after A -> **1,523** after A+B.

> The original brief said 4,945 sources / 1,036 left. That figure also counts the 487 never-stacked single ARWs,
> which belong to no bracket set. The CSVs give 4,458 (98+228+16+4060+56 over 49x2, 76x3, 4x4, 812x5, 8x7 frames),
> so the runner asserts 4,458 and 1,523.

Because A removes the **stacked** `-HDR-2.dng` (the stack parent), the survivor in those 26 sets is the unstacked
`-HDR.dng` (Adobe Color look). That follows the instruction; the analysis report had proposed the opposite.

## Run it (orchestrator only; plugin serves one client at a time)

Copy `scripts/catalog_cleanup.py` and the CSVs `hdr_outputs.csv bracket_sets.csv duplicate_merges.csv` to
`C:\NA2026\cl_scratch\` (`scp -O`). Use the desktop venv's Python (it has `lightroom_sdk` installed):

```
cd C:\NA2026\cl_scratch
$py = "$env:LOCALAPPDATA\lightroom-cli\venv\Scripts\python.exe"

# 1. dry run (default without --yes): connects, runs preflight, prints the plan, writes nothing
& $py catalog_cleanup.py --csv-dir . --step all

# 2. canary: 1 duplicate + 3 sources
& $py catalog_cleanup.py --csv-dir . --step A --yes --limit 1
& $py catalog_cleanup.py --csv-dir . --step B --yes --limit 3

# 3. the rest (re-runnable after any crash; finished work is skipped)
& $py catalog_cleanup.py --csv-dir . --step A --yes
& $py catalog_cleanup.py --csv-dir . --step B --yes
& $py catalog_cleanup.py --csv-dir . --step C --yes     # needs the plugin with develop.clearAutoTone
```

`--step all --yes` runs A, B, C in order. Options: `--limit N` (max write actions per step), `--log PATH`
(default `C:\NA2026\cl_scratch\catalog_cleanup.ndjson`), `--force-order` (skip the "previous step complete" check),
`--port-file`. Environment `LR_BRIDGE_HOST` / `LR_PORT_FILE` apply as for the SDK.

## Expected dry-run output

```
PLAN  (DRY RUN - nothing will be written)
  catalog now            : 6007 photos (975 merged DNG, 5032 ARW)
  A remove -HDR-2.dng    : 26 (partner -HDR.dng kept)
  B remove bracket ARWs  : 4458 from 949 merged sets
    kept: 84 ARWs of 23 merge-less sets, 3 focus-bracket frames, single ARWs
  C develop.clearAutoTone: all 1523 remaining photos, 25 per call
  expected after A+B     : 1523 photos (6007 - 26 - 4458)
  steps selected         : ABC
preflight ok: live catalog 6007 photos; all kept photos present; 20-id metadata samples match for steps AB
dry run: no writes performed. Re-run with --yes to execute.
```

Preflight (always, before any write) pings, pages the whole live catalog (`catalog.getAllPhotos`), requires 6,007
on a fresh run (on a resumed run: between 1,523 and 6,007 minus logged removals), checks **every** planned id
against its CSV filename, checks all photos to keep are present, and samples 20 ids per step with
`catalog.getPhotoMetadata`. Any mismatch aborts with exit code 2 before anything is written.

Postflight prints `SUMMARY A/B/C` (removed / already_gone / resumed / cleared / unchanged / skipped) and
`POSTFLIGHT live catalog: N photos` (expected 1,523 after A+B).

## Behaviour

- One `LightroomClient`, one connection, strictly sequential calls, 30 s timeout. Connection loss: the SDK
  reconnects; the runner additionally backs off (1,2,4,8,16,30 s) and reconnects the same client. A removal
  retried after a drop that answers `PHOTO_NOT_FOUND` counts as done.
- Step B confirms a set's surviving merge exists (`getPhotoMetadata`, filename match) before removing its sources.
- Log: NDJSON, one line per action: `ts, step, id, filename, action, result` (+ `changed` / `skippedReason` for C).
  Append-only, `fsync` per line; a dry run writes no log. Resume = re-run the same command.
- B refuses unless A is complete in the log, C unless B is (unless `--force-order`).
- Exit codes: 0 ok, 1 runtime failure, 2 plan/preflight assertion or refusal, 3 step C unavailable
  (`UNKNOWN_COMMAND`; A/B are unaffected).
- Step C assumes `develop.clearAutoTone` replies with a list of `{id, filename, changed, skippedReason}` (bare list or
  wrapped in a dict key such as `results`). Develop writes on non-virtual-copy photos are subject to the plugin
  Guard; if E4's command does not exempt this, expect per-photo `skippedReason` or a `GUARD_DENIED` abort.

Tests: `python -m pytest tests/cleanup` (mock plugin, includes a full rehearsal on the real CSVs when
`/projects/.lr-scratch/analysis` exists).
