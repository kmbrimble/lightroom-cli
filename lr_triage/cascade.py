"""Escalation decision, Claude call, merge. The API key is read from the environment only and never logged."""
import base64, io, json, os, urllib.error

from .classify import _post, parse_json
from .schema import LOW_CONF_FIELDS, PROMPT, SCHEMA

MODEL = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
PRICE_IN, PRICE_OUT = 2.0, 10.0  # USD per MTok, claude-sonnet-5-5 (claude-api skill table, 2026-10-06)
EST_IN, EST_OUT = 1300, 330      # per-photo token estimate for dry-run projections (1024px ~= 930 img + prompt)

# local visible_problems label -> lr_quality flag codes that corroborate it
LOCAL_TO_Q = {"blown_highlights": {"blown_highlights", "overexposed"}, "too_dark": {"underexposed"},
              "blurry": {"soft_focus"}, "motion_blur": {"soft_focus"}, "tilted_horizon": {"tilted_horizon"}}
Q_CODES = set().union(*LOCAL_TO_Q.values())


def load_quality(path):
    """{photo_id: record} from quality.ndjson (id = file stem of `source`). Missing file -> {}."""
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue  # partially written last line
                if r.get("source") and not r.get("error"):
                    out[os.path.splitext(os.path.basename(r["source"].replace("\\", "/")))[0]] = r
    return out


def escalation_reasons(local, quality, min_conf=0.60):
    f, conf, why = local["fields"], local["conf"], []
    for k in LOW_CONF_FIELDS:
        c = conf.get(k)
        if c is None:
            why.append(f"no_logprob:{k}")
        elif c < min_conf:
            why.append(f"low_conf:{k}")
    if f.get("people_count", 0) > 0 and f.get("is_close_up_person"):
        why.append("close_up_person")
    if quality is None:
        return why  # no metrics: local-only checks (the driver waits a grace period for quality first)
    flagged = {x["code"] for x in quality.get("flags", []) if x.get("severity") in ("warn", "bad")}
    says = {p for p in f.get("visible_problems", []) if p in LOCAL_TO_Q}
    why += [f"quality_contradicts:{p}" for p in sorted(says) if not (LOCAL_TO_Q[p] & flagged)]
    covered = set().union(*(LOCAL_TO_Q[p] for p in says)) if says else set()
    why += [f"quality_missed:{c}" for c in sorted((flagged & Q_CODES) - covered)]
    if (quality.get("needs_second_opinion") or {}).get("value"):
        why.append("quality_second_opinion")
    return why


def _jpeg_1024(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        from PIL import Image  # optional; without it the preview is sent as-is
    except ImportError:
        return raw
    im = Image.open(io.BytesIO(raw))
    im.thumbnail((1024, 1024))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def cost_usd(usage):
    return ((usage or {}).get("input_tokens", 0) * PRICE_IN + (usage or {}).get("output_tokens", 0) * PRICE_OUT) / 1e6


def claude_classify(image_path, api_key, url=API_URL, model=MODEL):
    """Forced tool_choice is a 400 on Sonnet 5.5, so: tool_choice auto + strict tool + instruction; text-JSON fallback."""
    tool = {"name": "record_triage", "description": "Record the triage labels for this photo.",
            "input_schema": SCHEMA, "strict": True}
    body = {"model": model, "max_tokens": 500, "thinking": {"type": "between_tools"},
            "tools": [tool], "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(_jpeg_1024(image_path)).decode()}},
                {"type": "text", "text": PROMPT + "\nCall the record_triage tool with your answer."}]}]}
    try:
        r = _post(url, body, {"x-api-key": api_key, "anthropic-version": "2023-06-01"}, timeout=120)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"anthropic http {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from None
    fields = None
    for b in r.get("content", []):
        if b.get("type") == "tool_use":
            fields = b["input"]
        elif b.get("type") == "text" and fields is None:
            try:
                fields = parse_json(b["text"])
            except ValueError:
                pass
    if fields is None:
        raise RuntimeError(f"no usable answer (stop_reason={r.get('stop_reason')})")
    return {"fields": fields, "usage": r.get("usage"), "cost_usd": round(cost_usd(r.get("usage")), 5)}


class Cascade:
    def __init__(self, quality, api_key=None, max_usd=8.0, dry_run=False, spent=0.0, **kw):
        self.quality, self.key, self.max_usd, self.spent, self.kw = quality, api_key, max_usd, spent, kw
        self.dry = dry_run or not api_key

    def run(self, photo_id, image_path, local):
        reasons = escalation_reasons(local, self.quality.get(photo_id))
        rec = {"photo_id": photo_id, "source": "local", "fields": local["fields"], "conf": local["conf"],
               "min_conf": local["min_conf"], "reasons": reasons, "escalate": bool(reasons), "local": local, "claude": None}
        if not reasons:
            return rec
        if self.dry:
            rec["escalation"] = "dry_run"
        elif self.spent >= self.max_usd:
            rec["escalation"] = "budget_exhausted"
        else:
            try:
                c = claude_classify(image_path, self.key, **self.kw)
            except Exception as e:  # keep the local answer; record why
                rec["escalation"] = "error: " + str(e)[:200]
            else:
                self.spent += c["cost_usd"]
                rec.update(source="claude", fields=c["fields"], claude=c, escalation="done")
        return rec
