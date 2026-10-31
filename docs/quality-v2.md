# lr_quality v2: learned, calibrated, trainable from reviews

v2 keeps every v1 field and adds (schema `lr_quality/2`): `features` (flat numeric features), `assessment` (per-issue
calibrated probability, severity 0-3, suggested fixes), model-driven `flags` and `needs_second_opinion`. The v1 rule output moves
to `flags_v1` / `needs_second_opinion_v1`; `--model none` gives exact v1 behaviour. Learning is a command, not a code edit.

## 1. "Fit for purpose", defined before tuning

Ground truth: Sonnet-5.5 reviews of every one of the 1,523 previews against `lr_quality/rubric.md` (`docs/quality-labels.md`).
"Noticeable" = label severity >= 2. Held-out = fixed 25% split, `sha1("lrq2:"+id) % 10000 < 2500`; it depends only on the id, so adding
labels never moves a photo across the split. The held-out rows are scored, never fitted; all choices (regularisation, Platt, thresholds,
second-opinion width) come from stratified, photo-grouped 5-fold CV on the other 75%.

| # | requirement | target |
|---|---|---|
| 1 | per flag (blown_highlights, crushed_shadows, underexposed, overexposed, soft_focus/blur, motion_blur) at label >= 2, held-out | precision >= 0.75 **and** recall >= 0.70 |
| 2 | tilt, photos with `tilt_confidence >= 0.6` | MAE <= 1 deg |
| 3 | second opinion | rate <= 15% of photos, and the flagged set contains most (>50%) of the photos with a flag error |
| 4 | speed | <= 200 ms/photo at 1600 px |
| 5 | learning | new reviews improve the model through `lr-quality learn`; a model worse on held-out is refused |

Label noise bound: the 40-photo repeat review agrees 95-100% per field (exact), so label noise does not cap the targets below
the above. (It measures one model's self-consistency, not bias: Claude is slightly lenient on bright water/white floors.)

## 2. Results (held-out n = 388, label >= 2; v1 = rule flags on the same photos)

Positives in the whole catalogue: highlights 122, shadows 33, focus 41, motion_blur 26, exposure <= -2: 4, tilt >= 1 deg: 10 (none >= 2).
Held-out positives are 7-35 per flag, so every number below has a wide interval (about +/-0.15 for n = 10-15).

| flag | n+ | v1 P / R | v2 P / R | fit? |
|---|---|---|---|---|
| blown_highlights | 35 | 0.37 / 0.94 | **0.83 / 0.69** (CV 0.75 / 0.74) | P yes, R 0.69 (marginal) |
| overexposed | 9 | 0.75 / 0.67 | 0.58 / 0.78 (CV 0.76 / 0.81) | borderline; n = 9 |
| crushed_shadows | 7 | 0.17 / 0.86 | 0.17 / 0.57 (CV 0.24 / 0.58) | **no**: 33 positives, hard |
| soft_focus (focus >= 2) | 12 | 0.75 / 0.25 | 0.44 / 0.33 | **no** |
| soft or motion blur ("blur") | 14 | 0.75 / 0.21 | 0.50 / 0.29 (CV 0.75 / 0.52) | **no** |
| motion_blur | 8 | n/a | 0.20 / 0.38 | **no** |
| underexposed | 1 | 0.07 / 1.0 | not measurable (4 positives in all) | **no data** |
| tilted_horizon | 0 | 238 flags | see section 5 | dropped by default |

Whole-catalogue flag counts, v1 -> v2: blown 338 -> 109, crushed 138 -> 42, soft_focus 267 -> 0 (+26 blur, 9 motion_blur),
tilted 238 -> 0 (opt-in), overexposed 28 -> 30, underexposed 71 -> 5. **Second opinion: v1 71.3% -> v2 15.3% of photos** (13.9% on
held-out; at that rate it contains 56% of the photos that have >= 1 flag error, 41% in dev).

Speed: 190 ms CPU/photo at 1600 px on a loaded server (v1 on the same box: 114 ms), numpy only.

**Verdict: fit for blown highlights (and close for overexposure); not fit for crushed shadows, focus/motion blur or underexposure
at P >= 0.75 and R >= 0.70.** Best reachable now is roughly P 0.5 / R 0.3 for blur: Sonnet judged blur on a 1280 px downscale of the
preview, so labelled "soft" often means subject-level softness (shallow depth of field, haze) that edge statistics cannot separate
from legitimate bokeh; and shadows >= 2 is a rare label (33) that the features separate with AUC 0.95 but not with precision.
AUC per issue is high (held-out: blown 0.97, overexposed 1.00, soft 0.94, crushed 0.95, blur 0.90), so ranking is good; the shortfall is
the operating point, which needs more positives to place reliably. Against v1, v2 is better or equal on F1 for blown (0.75 vs 0.53) and
blur (0.36 vs 0.33), and marginally lower for crushed (0.26 vs 0.29) and overexposed (0.67 vs 0.71), both within noise at n <= 9.

How the labels were used: round 1 (420 stratified photos, selected by v1 flags) drove feature design and the first fits; round 2 labelled
the remaining 1,103 photos, so the catalogue is now fully labelled and no reweighting is applied (`--population` detects full coverage;
with partial coverage it reweights by v1-flag stratum to estimate catalogue-wide precision). The split is by id, so round-1 and round-2
photos are mixed in both parts. Disclosure: held-out numbers were printed after each iteration, so decisions (threshold rule, adding
half-resolution focus features, tilt gating) were made while seeing them: treat held-out figures as mildly optimistic.
Triage `visible_problems` (331 escalated photos) were also tried as weak labels (weight 0.3); with full reviews they are ignored
(a real review always wins), so the shipped model uses none.

## 3. What changed in the analyser, and why

* **Highlights**: clipped area is split into connected regions (4x4 cell grid) and reported as `hi_big_pct` (blobs >= 0.25% of frame),
  `hi_small_pct` (specular dots, sun disc), `hi_interior_pct` (clipped with no detail), top-vs-lower thirds. A sparkle of clipping no
  longer looks like a blown sky.
* **Shadows**: `lo_pct`, `lo_pct_16/30`, and `lo_interior_pct` (whole 4x4 blocks <= 12).
* **Focus**: measured on the strongest-edge tiles (the in-focus subject), not a tile percentile that skies dilute; contrast-invariant
  fine/coarse gradient-energy ratio (x and y separately for motion blur), acutance, share of tiles near the image's own best tile,
  flat-tile fraction (sky), Laplacian at two scales.
* **Noise** and exposure percentiles, centre/lower-frame means.
* **Tilt**: structure-tensor orientation over long (21 px) coherent edges, magnitude and coherence weighted, +/-10 deg prior, with Hough
  and a vertical-edge estimate as extra inputs; ridge-regressed to the label convention (degrees clockwise to level), plus a gated
  classifier. Validated on synthetic rotations (below).
* **Learning**: numpy L2 logistic regression per severity head (>= 1, >= 2, >= 3) with Platt calibration on out-of-fold scores, regularisation
  by CV log-loss, photo-grouped stratified folds, precision-first operating threshold (highest recall with P >= 0.75 if it keeps recall >= 0.2,
  else best F1). An issue needs >= 8 trusted positives to get a model (underexposed currently has too few to be reliable).
* **Second opinion**: a photo is ambiguous when any issue's probability is within a log-odds distance `width` of its decision threshold;
  `width` is the largest value with <= 15% of dev photos flagged.

## 4. Commands

```
lr-quality features  PREVIEW_DIR --out feats.ndjson [--jobs 8]       # cache (ids = <id>.jpg stems)
lr-quality augment-tilt --reviews labels.ndjson --previews PREVIEW_DIR --out rot.ndjson   # synthetic tilt (optional)
lr-quality eval  --reviews labels.ndjson --features feats.ndjson [--aug rot.ndjson] [--model m.json] [--json out.json]
lr-quality learn --reviews labels.ndjson [--reviews more.ndjson] [--weak final.ndjson] --features feats.ndjson --out lr_quality/model.json
             [--add-review new.ndjson] [--force]
lr-quality analyze|batch ... [--model path|none] [--tilt]
```

* `eval` without `--model` fits on the dev 75% and prints CV (dev) plus held-out and dev tables with P/R/F1/AUC at severity >= 1/2/3, v1
  baseline columns, a second-opinion summary, tilt MAE, and (with `--meta review_set.json`) confusion by `kind` (hdr/arw) and scene.
* `learn` fits on dev, scores the held-out, compares with the previous model's embedded `dev_model` on the **same current held-out rows**,
  refuses (exit 1) if mean AUC drops > 0.01 or mean F1 > 0.02 (`--force` overrides), then refits on all labels and writes `model.json`.
  `--add-review new.ndjson` appends the file to the first `--reviews` file and retrains in one step; the JSON report shows before/after.
* `model.json` holds only coefficients (no ids), is package data, and is overridable with `--model`. New photos need `features` first.
* Review formats (auto-detected per line): Sonnet rubric rows (`id, highlights, shadows, exposure, focus, motion_blur, noise,
  horizon_tilt_deg, tilt_confidence`, optional `kind`, `scene_type`, `weight`); user verdicts `{"id", "verdict": "accept"|"reject",
  "issues": ["soft_focus", ...]}` (accept = all clear, reject = the named issues noticeable); and, with `--weak`, the triage
  `final.ndjson` (only rows with a Claude opinion; `visible_problems` blown_highlights / too_dark / blurry / motion_blur / tilted_horizon
  become severity-2 positives, absence a 0 for those issues, weight 0.3, never overriding a real review, never used for evaluation).
  A vision-model's output converts the same way: emit rubric rows, or a verdict row, with a `weight`.

## 5. Tilt: dropped by default

Evidence: Sonnet reported |tilt| >= 1 deg on 10 of 1,523 photos and >= 2 deg on none (v1 flagged 238, so ~all were false alarms, e.g.
tree lines, reflections). With no positive labels the flag cannot be validated or trained against real data. On synthetic data
(level-reviewed photos rotated by +/-1.2, 2, 3.5 deg), the estimator's MAE is 1.5 deg (v1 Hough 1.7; 38% within 1 deg) and the
precision-first flag fires on ~23% of rotated photos with a ~2% false-alarm rate on real level photos: it works only when strong straight
edges exist. MAE against real labels is 0.53 deg, but a predict-zero baseline scores 0.01, so that number only says "photos are level".
So `tilted_horizon` is not emitted unless `--tilt` / `tilt_flag=True`; `assessment.tilt.rotate_cw_deg` is always present as an estimate,
`flags_v1` still has the old rule. Target 2 is therefore met trivially and not meaningful; revisit when Kieren supplies real tilted shots.

## 6. Limits and what would help

* Positives are the limit: shadows >= 2 (33), focus >= 2 (41), motion >= 2 (26), underexposed (4). A few dozen more clear
  examples of each (especially genuinely soft/shaken keepers vs deliberate shallow depth of field) would move the operating points; Kieren's
  own accept/reject on these via `verdict` rows is the best source. Real tilted photos (>= 2 deg) are needed to re-enable the tilt flag.
* Labels are one model's opinion at 1280 px; calibrate against Kieren's own picks when available.
* The `hdr` vs `arw` split is reported by `eval --meta`; the model does not use it as a feature.
