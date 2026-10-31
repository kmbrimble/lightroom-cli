# triage-vlm: local VLM classification with escalation to Claude

Package `lr_triage/` (stdlib only; Pillow optional, used to downscale to 1024 px for Claude), driver `scripts/triage_classify.py`,
tests `tests/triage_vlm/` (mock HTTP servers; pass on Python 3.11 and 3.14, run with `--noconftest -c /dev/null` outside the dev venv).

**Status (2026-10-31): NOT yet run against the real model.** The vision server refused to start because only ~3.0 GiB VRAM was free
(the :8080 27B server, pid 14484, plus Lightroom/desktop hold the rest; `start-vision.ps1` needs >= 3500 MiB). Everything below
about measured speed, label precision and confidence calibration is therefore unmeasured. Only the mocked tests have run.

## Flow
1. `classify.local_classify`: POST to `127.0.0.1:8081/v1/chat/completions` with `response_format: json_schema` (falls back to
   `json_object` on HTTP 400, retries twice on unparseable output), `logprobs: true, top_logprobs: 3`, temperature 0.
2. Confidence = `exp(sum of token logprobs)` over every token overlapping the field's JSON value (`scene_type`, `has_people`,
   `is_close_up_person`, `visible_problems`). For a multi-token enum this is the path probability, so a sibling split at the first
   differing token is captured. `min_conf` = min over those four. Untested on the real server: whether llama.cpp reports logprobs
   for grammar-constrained tokens as raw or renormalised probabilities. If renormalised, confidences will read too high.
3. `cascade.Cascade`: escalate when `scene_type`/`has_people`/`visible_problems` confidence < 0.60; local names a problem
   lr_quality does not flag (`quality_contradicts:*`) or lr_quality flags one local missed (`quality_missed:*`); lr_quality
   `needs_second_opinion`; people_count > 0 with a close-up person. Mapping: blown_highlights<->blown_highlights/overexposed,
   too_dark<->underexposed, blurry/motion_blur<->soft_focus, tilted_horizon<->tilted_horizon (warn/bad severity only).
   `crushed_shadows`/`noisy` are not mapped. Expect `tilted_horizon` and `soft_focus` to dominate escalations until thresholds are
   calibrated (lr_quality's own docs say they are unvalidated on real photos).
4. Claude call: `claude-sonnet-5-5`, image base64 JPEG at 1024 px long edge, `max_tokens` 500, strict tool `record_triage` with the
   same schema. **Forced `tool_choice` returns a 400 on Sonnet 5.5**, so it uses `tool_choice: auto` plus an instruction naming
   the tool; `thinking: {type: "between_tools"}` keeps thinking off. Not yet exercised against the real API.
   Cost from `usage` at $2 in / $10 out per MTok (claude-api skill price table, cached 2026-10-06). `--max-usd` (default 8) stops
   escalation once reached (records `budget_exhausted`; the local answer stands). Spend is rebuilt from the output file on resume.
5. Key: env `ANTHROPIC_API_KEY` only; never logged or written. No key (true in this session's container; unchecked on the desktop)
   or `--dry-run-escalation` -> only records `escalate` + `reasons` (`escalation: dry_run`).

Output record (NDJSON): `photo_id, source (local|claude), fields (merged answer), conf, min_conf, reasons, escalate, escalation,
local, claude, quality_missing`.

## Running (on the desktop)
```
# server: start through an interactive scheduled task (the server dies with a short ssh session)
python scripts\triage_classify.py --watch --until-count 1523 --max-minutes 50        # venv python at %LOCALAPPDATA%\lightroom-cli\venv
```
Resumable (skips ids already in `triage.ndjson`), waits up to `--quality-grace` (300 s) per photo for its lr_quality record without
blocking the queue, stops on `previews\render.done`, `--until-count`, or `--max-minutes`, and writes `triage_report.md`
(scene counts, escalation rate and reasons, projected cost for 1,523 photos; the projection is token-estimated until a real Claude
call has been made).

## Open questions
- VRAM: the 27B on :8080 leaves < 3.5 GiB. Needs the 27B stopped/unloaded by its owner, or a lower threshold in `start-vision.ps1`
  (partial offload is slower and competes with Lightroom). I did not work around the guard.
- Real-model checks still to do: json_schema accepted? logprobs on constrained tokens raw vs renormalised? speed (~5 s/image
  expected)? does low `min_conf` track wrong labels?
