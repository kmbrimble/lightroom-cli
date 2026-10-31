# Ground-truth quality labels

Claude-reviewed labels for calibrating `lr_quality` against real problems on the trip photos. Rubric: `lr_quality/rubric.md`.
Script: `scripts/quality_review.py` (`build-set`, `review`, `agree`). Tests: `tests/quality_labels/`.

## Files (`/projects/.lr-scratch/quality-labels/`)

| file | content |
|---|---|
| `previews/<id>.jpg` | all 1,523 previews (1600 px long edge), pulled with `scp -O` |
| `quality.ndjson` | lr_quality output for every preview (desktop copy) |
| `final.ndjson` | scene labels (local model, Claude for 331 escalated) |
| `review_set.json` | seeded stratified review set: `{seed, n, photos:[{id, stratum, kind, filename, scene_type, flags}]}` |
| `labels.ndjson` | one row per reviewed photo: `id, filename, kind, <rubric fields>, usage, model` |
| `labels_repeat.ndjson` | second independent review of a random subset (same row schema) for the consistency check |

`kind` is `hdr` (id in `analysis/hdr_outputs.csv`, filename `...-HDR.dng`) or `arw` (everything else; `filename` is null because
the id->filename list for singles is only in the Lightroom catalog, which this work does not touch).

## Method

* Strata (each photo in exactly one, rarest first so rare flags are not starved): `dark` 60 (underexposed|crushed_shadows),
  `tilted` 40, `soft` 100, `blown` 120 (blown_highlights|overexposed), `noflag` 100 (no lr_quality flags at all, for false
  negatives). Half of each stratum is chosen round-robin over kind x scene x severity (guarantees coverage), half at random.
  Seed 20261031. `flags` in `review_set.json` is the lr_quality flag->severity map, so QI can see why a photo is there.
  **The review set is deliberately not representative**: reweight by stratum before quoting catalogue-wide rates.
* Reviewer: `claude-sonnet-5-5`, image downscaled to 1280 px JPEG, strict tool call, max_tokens 700, concurrency 3, retries
  with backoff, resumable. The prompt sees only the picture (no lr_quality output, no scene labels), so labels are independent.
* Sign convention: `horizon_tilt_deg` is degrees to rotate **clockwise** to level; `~= -lr_quality tilt.angle_deg`.
* Cost is measured from `usage` tokens at `lr_triage.cascade.PRICE_IN/PRICE_OUT`; cap $9 across both label files.

## Run

```
python3 scripts/quality_review.py build-set
hl exec --secret ANTHROPIC_API_KEY=claude-code/anthropic_api_key -- python scripts/quality_review.py review
hl exec --secret ANTHROPIC_API_KEY=claude-code/anthropic_api_key -- python scripts/quality_review.py review --repeat 40
python3 scripts/quality_review.py agree
```
(`python` needs Pillow.)

## Results (2026-10-31 run; 419 of 420 labelled, ~$4.1 + $0.39 repeat)

* 880392 failed repeatedly with `stop_reason=max_tokens` (long note) and is unlabelled; 880774 succeeded on rerun.
* Repeat-review agreement (40 random photos, independent second pass): exact match highlights 100%, focus 100%, motion_blur 100%,
  shadows 95%, noise 97.5%, exposure 97.5%, keeper_score 97.5% (all within 1: 100%); tilt null/non-null agreement 100%, mean |delta|
  0.014 deg on the 7 pairs with a value; fixes Jaccard 0.95. This is a ceiling on label noise, not on label bias: the sample is
  mostly clean photos with many zeros, and a second pass by the same model shares its blind spots.
* Claude almost never reports a tilt: only 3/419 labels have |horizon_tilt_deg| >= 1 (and 254 are null). In eyeballing, the
  `tilted_horizon` flags on the sampled photos were false positives (reflections, tree lines), so this looks right, but QI
  should treat tilt as "lr_quality angle vs 0" rather than expecting many positives.
* Claude is slightly conservative on highlights for bright-water / blown-white floor shots (labels 1 where a human would say 2).
* Against lr_quality flags (by stratum): blown_highlights stratum 35/120 labelled highlights>=2; soft stratum 35/100 labelled focus>=1 or
  motion_blur>=1 (11/100 focus>=2); dark stratum 32/59 shadows>=2 or exposure<=-1; tilted stratum 0/40 with |tilt|>=1; noflag stratum
  3/100 with a clear problem (highlights/shadows/focus>=2 or |exposure|>=2). Low flag precision, low false-negative rate.
