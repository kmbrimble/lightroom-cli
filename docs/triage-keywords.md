# AI keywords and smart collections from triage

Writes descriptive keywords (from `final.ndjson`) and `AI - *` smart collections into Lightroom through the daemon.
Only two write commands are used: `catalog.addKeywords` and `catalog.createSmartCollection`. No flags, ratings, labels,
titles, develop or removals. No problem/quality keywords are written (visible_problems, `tilted_horizon`, lr_quality).

## Pipeline

1. `python scripts/triage_keywords.py` -> `keyword_plan.ndjson` (offline; rules in `lr_triage/keywords.py`).
2. Copy `scripts/triage_apply.py`, `scripts/undo_ai_keywords.py` and the plan to the desktop (`scp -O`), then with the venv python:
   `dry` (counts + 10 samples; `--live` also lists collisions with existing catalog keywords), `canary --ids a,b,c,d,e`
   (pre/post `getPhotoMetadata`: exactly the planned keywords arrived, every other metadata field identical),
   `run [--limit N]` (sequential, resumable from the NDJSON log, retry once, aborts at >2% errors after 50 calls, on 5
   consecutive errors, on BRIDGE_DOWN/GUARD_DENIED, or when daemon health is not `connected`), `collections [--only kw]`,
   `report [--only kw]`, `verify [--sample 200]`. Run `run` in chunks (`--limit 400`): Windows sshd does not kill children.
3. The log records a `baseline` line (catalog keyword photo counts before the first write), a `start` line per photo with the
   exact `to_add` (keywords the photo did not already have) and an `ok`/`error` line. Undo and verify are driven by it.

## Vocabulary rules

- Lowercase, `_`->space; synonym map (clouds->cloud, trees->tree, fall->autumn, canon->canyon, blue sky->sky, cloudy->overcast,
  evergreen->conifer, person->people ...), and plural folding (X+s -> X when X is itself a keyword).
- Dropped: meta words (`landscape` only where the scene keyword implies it, `nature`, `outdoor`, `travel`, `scenic`, `view`),
  colour-only (`blue`, `red`, `golden`, `turquoise`...), mood-only (`calm`, `moody`, `still`, `dark`), technique/quality
  noise (`blur`, `bokeh`, `low light`, `blown highlights`, `shallow depth of field`), people appearance (`puffer jacket`,
  `long hair`, `smiling`, `woman`, `selfie`), `tilted_horizon`.
- A keyword must be on >= 3 photos (after merging); scene and people keywords are exempt. Max 8 descriptive keywords per photo,
  in the model's order.
- Per photo: scene keyword (mountain_landscape->mountain, coast_beach->coast, garden_flowers->garden, city_skyline->city,
  people_portrait->portrait, sky_clouds->sky, other-> none), `canyon` when subjects/keywords mention canyon (also catches the
  ~70 slot canyons relabelled `other`; `slot canyon` and `red rock` stay as their own keywords), `people` when has_people,
  `group` when people_count >= 4, descriptive keywords, then the marker `AI tagged`.
- Species rule: for final source `local` with min_conf < 0.6, species keywords (bear, deer, squirrel, ...) are dropped. In this
  data it matched **0 photos**: every low-confidence local row had already been escalated to Claude.

## Smart collections

`createSmartCollection` takes `{name, searchDesc}`; searchDesc is a Lightroom criterion table, here
`{criteria="keywords", operation="any", value=<kw>}`. `any` is a *contains* match, so e.g. `city` also matches `city view`.
Expected counts therefore use substring matching. The plugin passes `returnExisting=true`, so re-running does not duplicate,
but also does not update criteria. Tested first on `AI - Waterfall`: getCollections count == getCollectionPhotos total == expected.

## Quirks

- `addKeywords` creates keywords with `includeOnExport=false`: they stay in the catalog but are NOT written into exported
  JPEG/XMP metadata until "Include on Export" is ticked per keyword (Keyword List panel, right-click > Edit Keyword Tag).
  Good for `AI tagged`; for the descriptive ones that is a choice for the owner.
- `catalog.getKeywords` photoCount is the readback source; keywords are flat (no hierarchy).

## Undo

```
<venv python> C:/NA2026/cl_scratch/undo_ai_keywords.py --log C:/NA2026/cl_scratch/run.ndjson --lrc C:/Users/Kieren/AppData/Local/lightroom-cli/lrc.py [--dry-run]
```

Removes exactly each photo's logged `to_add` through `catalog.removeKeyword` (pre-existing keywords are never touched;
KEYWORD_NOT_FOUND counts as done; resumable via `undo_ai_keywords_log.ndjson`). The emptied keyword objects and the `AI - *`
smart collections stay and are deleted by hand.

## Round 2: review and fix keywords (from quality-labels/labels.ndjson)

`python scripts/triage_review_plan.py` -> `review_plan.ndjson` (485 photos get at least one keyword; the other 1,038 get none, no marker).
Rules in `lr_triage/review_keywords.py`, applied exactly as specified: Blown highlights (highlights>=2), Crushed shadows (shadows>=2),
Too dark (exposure<=-1 and fixes has raise_exposure/lift_shadows, or exposure<=-2), Too bright (exposure>=2 or lower_exposure),
Soft focus (focus>=2), Motion blur (>=2), Needs straightening (|tilt|>=1 deg and confidence>=0.6), Reject candidate (keeper<=2),
Top pick (==5), Uncertain, Possibly intentional (carries `canyon`/`slot canyon` from round 1 and Motion blur or Blown highlights).
Fix hints come from `fixes` only when the matching severity is >=2 (exposure: <=-1 / >=1; crop: no metric) or keeper_score<=3.
Same tooling, separate log (`run_review.ndjson`, own baseline). The writer ran as ONE detached Scheduled Task
(`LrReviewKeywordsOnce`, a .cmd wrapper redirecting to `run_review.out`, deleted afterwards); the script also refuses to start when
`<log>.lock` exists, so two writers on one log are impossible. `--only` takes a keyword with `_` for spaces (ssh quoting).

**Smart collection gotcha:** Lightroom's `any` operator splits the value into words and matches ANY of them. `Review - Top pick`
as the value matched all 485 Review-tagged photos. Review collections therefore use one word that occurs in no other keyword
(`Blown`, `Crushed`, `dark`, `bright`, `Soft`, `blur`, `straightening`, `Reject`, `pick`, `Uncertain`, `Possibly`; a test checks
uniqueness). `Fix - *` hints get no collection because their words are shared with Review keywords.

Undo for both runs: `undo_ai_keywords.py --log run.ndjson run_review.ndjson` (one process, one undo log).

## Tests

`python -m pytest tests/triage_apply` (real daemon + stateful mock plugin: canary, resume, collections, verify, undo, abort).
