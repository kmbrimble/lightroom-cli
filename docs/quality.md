# lr_quality: exposure / focus / tilt / noise metrics

> **v2 (schema `lr_quality/2`)**: learned, calibrated per-issue probabilities, severities, suggested fixes and a model-driven
> second-opinion rule, trained from reviews with `lr-quality learn`. See [quality-v2.md](quality-v2.md). The v1 rule engine below is
> still available (`--model none`) and its flags are kept as `flags_v1`.

`lr_quality` turns a JPEG into one structured JSON object of cheap, deterministic image metrics, so a caller
(a local vision-model pipeline, a daemon, a script) can pre-review ~1,000 photos and escalate only the doubtful ones.
CLI: `lr-quality` (or `python -m lr_quality`). Python: `lr_quality.analyze(...)`.

## Design decision

This is a **separate Python package at the repo root (`lr_quality/`), not a Lightroom plugin**.

- Lightroom plugins are Lua; they have no numpy or imaging libraries, and the SDK cannot give a plugin pixel arrays cheaply.
- The metrics need no Lightroom API, only pixels: a JPEG preview from Lightroom, or an embedded/exported JPEG.
- It must be callable from a CLI, from Python, or by other tools (the `lrd` daemon, a pipeline, a cascade that decides when to ask a
  bigger model). A plugin command would tie it to the single-client live bridge (see CLAUDE.md constraints).
- It imports neither `lightroom_sdk` nor `pydantic` (a test enforces this) and never talks to Lightroom.

Dependencies: `numpy` and `Pillow` required (`pip install -e '.[quality]'`). `opencv-python-headless` is an optional accelerator
(`'.[quality-fast]'`); it only replaces the 3x3 filters, and results match the numpy path to float tolerance (tested). Use
`--no-cv2` / `accel="numpy"` to force the pure path. Runs on Linux Python 3.11 and Windows Python 3.12 (no platform-specific code;
Windows itself is unverified, see the end).

## Usage

```
lr-quality analyze preview.jpg [--scene-hint landscape|city|people|sunset|snow|interior|night]
                               [--thresholds t.json] [--targets targets.json] [--long-edge 1600] [--no-cv2]
cat preview.jpg | lr-quality analyze -            # bytes on stdin
lr-quality batch a.jpg b.jpg dir/ | --list files.txt | --stdin-list [--out results.ndjson]
lr-quality learn-targets ~/best_edits [--out targets.json]
```

- `analyze` prints ONE JSON object. `batch` prints NDJSON (one object per image, input order, folders expanded recursively) or
  writes it to `--out`. A failing image becomes `{"schema":..., "source":..., "error":"..."}`; the batch carries on.
- Exit codes: 0 ok, 1 some image failed, 2 usage error. stdout carries only JSON; diagnostics go to stderr.
- `batch` adds batch-relative fields in a final pass and **re-evaluates flags** with them, so run a whole shoot (or a
  meaningful chunk of it) in one batch for the best blur judgement.
- Piping: `lr-quality batch --stdin-list --out r.ndjson < paths.txt`, then `jq -c 'select(.needs_second_opinion.value)' r.ndjson`.
  From Python: `r = lr_quality.analyze(jpeg_bytes, scene_hint="landscape")`. Analysis is single-threaded; parallelise by running several
  `analyze` processes, but note that batch-relative fields only exist in `batch`.

Analysis runs on a copy resized (downscale only, Lanczos) to a long edge of 1600 px (`--long-edge`) so focus scores are
comparable across inputs. Smaller images are not upscaled (`analysed_long_edge` reports the real value). EXIF orientation is applied.
Luma is Pillow's integer Rec.601 conversion (0-255). Output floats are rounded to 4 dp; the output is deterministic.

## Output schema `lr_quality/1` (v1 fields; `lr_quality/2` only adds keys, see quality-v2.md)

| Field | Meaning |
|---|---|
| `schema` | `"lr_quality/2"` (v1: `"lr_quality/1"`) |
| `source` | path as given (null for stdin/bytes/PIL) |
| `scene_hint` | echo of the hint, or null |
| `image.width`, `image.height` | original size (after EXIF rotation) |
| `image.analysed_long_edge` | long edge actually analysed |
| `image.format` | Pillow format (`JPEG`, ...) or null |
| `exposure.mean_luma` | mean of 0-255 luma |
| `exposure.p01 p05 p25 p50 p75 p95 p99` | luma percentiles (integers, 0-255; smallest value with cumulative fraction >= p) |
| `exposure.clip_high_pct` | % of pixels with luma >= 250 |
| `exposure.clip_low_pct` | % of pixels with luma <= 5 |
| `exposure.clip_high_pct_by_channel` | `{r,g,b}`: % of pixels with that channel >= 250 |
| `exposure.upper_third_clip_high_pct` | `clip_high` computed over the top third of the frame only (sky/sun heuristic) |
| `exposure.midtone_mass` | fraction of pixels with luma 64-192 |
| `exposure.histogram_luma` | 32 bins of 8 levels, normalised to sum 1 |
| `exposure.dynamic_range_p99_p01` | `p99 - p01` |
| `exposure.vs_targets` | null, or `{scene, delta:{mean_luma,p01..p99}}` = this image minus the learned target (needs `--targets`) |
| `exposure.batch_percentile` | percentile rank (0-100) of `mean_luma` within the batch; null for single `analyze` |
| `focus.score_tile_p90` | 90th percentile, over an 8x8 tile grid, of the variance of the 3x3 Laplacian of luma. Skies are legitimately low-texture, so the sharpest tiles represent the image, not the mean |
| `focus.score_global` | Laplacian variance over the whole frame (drops with big smooth areas; used for conflict detection) |
| `focus.sharp_tile_fraction` | fraction of tiles with Laplacian variance >= `focus.sharp_tile_var` (40, absolute) |
| `focus.method` | `"laplacian_variance_tiles"` |
| `focus.relative_to_batch` | `score_tile_p90 / batch median`; null for single analyse or batches smaller than `focus.min_batch` (5) |
| `tilt` | null when no reliable horizon, else `{angle_deg, confidence, lines_used}`. Hough transform over strong, near-horizontal edges (|angle| <= 10 deg) in the 15-70% height band at <= 800 px. `angle_deg` > 0 means the horizon descends to the right (counter-clockwise rotation fixes it). `lines_used` = distinct long lines agreeing within 1 deg; `confidence` = min(1, lines_used/3) x vote share of agreeing lines |
| `noise.sigma_estimate` | Immerkaer estimate of noise sigma (0-255 luma scale, at the analysed size) over the flattest quarter of non-saturated tiles; null if no usable tile. Textured scenes without flat areas over-estimate |
| `flags[]` | `{code, severity: info\|warn\|bad, value, threshold, message}` for `blown_highlights, crushed_shadows, underexposed, overexposed, soft_focus, tilted_horizon, noisy`. Sorted worst first. (`info` is reserved; none emitted yet) |
| `needs_second_opinion` | `{value: bool, reasons: [str]}`, see below |

Targets (`learn-targets`): point it at a folder of your best edits. Subfolder name = scene class (`landscape/`, `city/`, ...);
loose files go into `default`, which otherwise is the pooled median. It writes `{"schema":"lr_quality_targets/1","scenes":{class:{n,mean_luma,p01..p99}}}`
using per-class medians. Analysis picks the `--scene-hint` class, else `default`. This is deliberately learned from the user's own
work; there is no built-in "ideal histogram". (Note `vs_targets.delta` is an object, `{scene, delta}`, not a bare delta map.)

## Thresholds (`lr_quality/thresholds.json`)

**Conservative heuristics, not photographic authority.** Override any key per call (`thresholds={"luma":{"under_warn":0.15}}` or
`--thresholds file.json`, deep-merged onto the defaults). Absolute blur is domain dependent, so inside a batch blur is judged
relative to the batch median.

| Rule | warn | bad | Notes |
|---|---|---|---|
| `blown_highlights` | `clip_high_pct` > 1.5 | > 5 | research pass: ~1-2% |
| `crushed_shadows` | `clip_low_pct` > 5 | > 15 | |
| `underexposed` | mean luma/255 < 0.19 | < 0.12 | research pass: ~0.18-0.2 |
| `overexposed` | mean luma/255 > 0.75 | > 0.85 | assumed mirror of the above |
| `soft_focus` (batch) | `relative_to_batch` < 0.28 | < 0.15 | research pass: 25-30% of median |
| `soft_focus` (single) | `score_tile_p90` < 8 | < 3 | absolute fallback; always requests a second opinion |
| `noisy` | sigma > 5 | > 9 | assumed; unvalidated on real photos |
| `tilted_horizon` | 0.4 <= abs(angle) <= 4 deg and >= 2 agreeing lines | n/a | suggestion only |

Values marked "assumed" and the absolute focus numbers were set on synthetic images and need calibration on real photos.

### `needs_second_opinion` is true when
- a metric is within `second_opinion.margin` (25%) of its warn threshold (either side), or the tilt is near 0.4 / 4 deg;
- tilt is in range but seen from too few lines;
- signals conflict: high tile sharpness but `score_global` < 0.2 x tile-p90 (small sharp area), or blown highlights together with
  crushed shadows / underexposure;
- the scene hint makes a rule unreliable (`second_opinion.scene_unreliable`, e.g. `sunset` for highlight clipping, `snow` for
  exposure, `night` for dark/noise) and that rule fired or is near its threshold;
- the highlight clipping is concentrated in the upper third (sky/sun);
- the image is unusual: long edge < 256 px, or `p99 - p01` < 8 (almost uniform).

`reasons` lists human-readable strings; match on substrings loosely, not exactly.

## Cascade consumer (prose only; not implemented here)

A caller sends each preview through the local vision model first and calls `lr-quality batch` over the same previews. It trusts the
local model's verdict when the model is confident AND `needs_second_opinion.value` is false. It escalates the image (preview plus the
metrics JSON and reasons) to a larger model when `needs_second_opinion.value` is true or the local model reports low confidence. A
`bad` flag with no second-opinion request is a strong enough signal to reject without escalating; a `warn` flag is only a hint
to the local model's prompt ("highlights clip 3% in the upper third"). The metrics inform the models; they never replace them
for scenes where the rules are known to mislead (sunsets, snow, night, small sharp subjects).

## Performance

Measured on an Intel i9-9900K, Linux, Python 3.11, numpy 2.4, single thread, JPEG bytes in (decode + analyse), median of 15:

| Input | cv2 present | numpy only |
|---|---|---|
| 1024x683 | 28 ms | 32 ms |
| 1600x1067 | 76 ms | 130 ms |
| 3000x2000 (resize included) | 153 ms | 173 ms |

Feed Lightroom previews (<= 2048 px) for the fast path; full-size JPEG decode dominates for big files. Not measured on the
Windows desktop.

## Limits

Unverified on real photos: threshold values, tilt on real horizons (waves, buildings, trees give false lines), noise estimates on
real JPEG chroma processing, Windows 3.12. Tests use synthetic images only. Tilt looks only for lines within 10 degrees of
horizontal and cannot distinguish a tilted horizon from a deliberately sloping subject (roofs, hillsides).
