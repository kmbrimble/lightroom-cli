# triage-vlm: local VLM classification with escalation to Claude

Package `lr_triage/` (stdlib only; Pillow optional, used to downscale to 1024 px for Claude), desktop driver
`scripts/triage_classify.py` (local pass), container script `scripts/triage_escalate.py` (Claude pass), tests `tests/triage_vlm/`
(mock HTTP servers; pass on Python 3.11 and 3.14 -- no 3.12 on the dev box; run with `--noconftest -c /dev/null` outside the dev venv).

Split: the desktop never sees the API key. Desktop: previews -> local VLM -> `triage.ndjson` (records `escalate` + `reasons`).
Container: `hl exec --secret ANTHROPIC_API_KEY=claude-code/anthropic_api_key -- python3 scripts/triage_escalate.py --pull`
pulls `triage.ndjson`, `quality.ndjson` and the escalated previews (`scp -O`) into `/projects/.lr-scratch/triage/`, calls Claude,
writes `final.ndjson`. Key is env-only, scrubbed from error text, never on argv/disk.

## Flow
1. **Local pass** (`classify.local_classify`, desktop): llama-server `:8081`, `response_format: json_object`, temperature 0, logprobs on.
   Enums are spelled out in the prompt with a format example; the answer is validated afterwards (retry at temp 0.3 on malformed
   JSON; an off-enum `scene_type`/`visible_problems` is coerced to `other`/dropped and given confidence 0 plus an `off_enum:*` reason).
   **Do not use a json_schema grammar here**: llama.cpp reports the model's raw distribution, not the grammar-renormalised one, so a
   grammar-forced enum token gets a meaningless tiny logprob (seen live: model wanted "landscape" at 99.8%, grammar forced "city" at
   e^-7, every photo got min_conf 0.0). Free generation also produced broken arrays once without the format example, hence the
   type validation.
2. **Confidence** = `exp(sum of token logprobs)` over the tokens covering the field's JSON value (path probability, so sibling enum
   labels that split at the first token are captured). `min_conf` = min over scene_type, has_people, is_close_up_person,
   visible_problems.
3. **Escalation decision** (`cascade.escalation_reasons`, re-evaluated container-side from the stored local answer + quality.ndjson):
   - `low_conf:{scene_type,has_people,visible_problems}` (< 0.60), `off_enum:*`, `close_up_person` (people_count > 0 and close-up).
   - `quality_contradicts:P`: local names problem P (blown_highlights<->blown/overexposed, too_dark<->underexposed,
     blurry/motion_blur<->soft_focus, tilted_horizon<->tilted_horizon) and lr_quality has no warn/bad flag for it.
   - `quality_missed:C`: lr_quality flags C at severity **bad** (blown_highlights, overexposed, underexposed, soft_focus) and local
     did not name the matching problem. Warn-level misses are ignored (near-threshold only); tilted_horizon is never a miss.
   - **lr_quality `needs_second_opinion` is not used**: it is true for 71% of this catalog (mostly "tilt: too few lines", upper-third
     clipping, near-threshold margins) and would escalate most photos.
4. **Claude pass** (`scripts/triage_escalate.py`, container): `claude-sonnet-5-5`, base64 JPEG downscaled to 1024 px (needs Pillow:
   run it from a venv with `pillow`; without it the full 1600 px preview is sent and input tokens go 2,384 -> 4,001 per photo),
   `max_tokens` 500, strict tool `record_triage` (`strict_schema()`: Claude rejects `minimum`/`maxLength`/`minItems>1`), `tool_choice: auto`
   because forced tool choice is a 400 on Sonnet 5.5, `thinking: {type: "between_tools"}`. Transient API errors are not written, so a
   rerun retries them. `--max-usd` (default 8) stops escalation (`budget_exhausted`, local answer kept); spend is rebuilt from
   `final.ndjson` on resume. `--force N` escalates the N lowest-confidence photos (test only). No key or `--dry-run-escalation`: records only.
   **Prices assumed**: $2.00 input / $10.00 output per million tokens (Sonnet 5.5 row of the claude-api skill's model table, cached
   2026-10-06; no prompt-cache discount, no batch discount). Cost per photo is computed from the response `usage` tokens
   (`claude.cost_usd`, alias `claude.usd`).

Output record (NDJSON): `photo_id, source (local|claude), fields (merged answer), conf, min_conf, reasons, escalate, escalation,
local, claude, quality_missing`.

## Running
```
# desktop (interactive scheduled task; the server dies with a short ssh session). venv python has no extra deps:
python scripts\triage_classify.py --watch --until-count 1523 --max-minutes 150
# container
hl exec --secret ANTHROPIC_API_KEY=claude-code/anthropic_api_key -- /projects/.lr-scratch/triage/venv/bin/python scripts/triage_escalate.py --pull
python3 scripts/triage_summary.py        # stats over final.ndjson
```
