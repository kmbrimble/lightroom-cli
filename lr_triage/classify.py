"""Local pass: Qwen3-VL via llama-server (OpenAI-compatible), free JSON generation, honest logprob confidences."""
import base64, json, math, urllib.error, urllib.request

from .schema import CONF_FIELDS, PROMPT, SCHEMA


def _post(url, body, headers=None, timeout=300):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def parse_json(text):
    """Tolerant: first balanced-looking {...} in the text."""
    try:
        return json.loads(text)
    except ValueError:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise
        return json.loads(text[i:j + 1])


def field_confidences(content, tokens, fields=CONF_FIELDS):
    """P(chosen value) per field = exp(sum logprob of every token overlapping the value's characters).
    For a multi-token enum that is the path probability, i.e. the product of the first-differing-token
    probability (siblings like sky_clouds/city_skyline split at token 1) and the near-1 continuation tokens.
    `tokens` = llama.cpp/OpenAI logprobs.content list. Returns {} if tokens don't re-assemble to `content`."""
    if not tokens or "".join(t["token"] for t in tokens) != content:
        return {}
    spans, pos = [], 0
    for t in tokens:
        spans.append((pos, pos + len(t["token"]), t["logprob"]))
        pos += len(t["token"])
    dec, out = json.JSONDecoder(), {}
    for f in fields:
        k = content.find(f'"{f}"')
        if k < 0:
            continue
        s = content.index(":", k) + 1
        while content[s] in " \n\t":
            s += 1
        try:
            _, e = dec.raw_decode(content, s)
        except ValueError:
            continue
        lp = sum(l for a, b, l in spans if a < e and b > s)
        out[f] = round(math.exp(min(lp, 0.0)), 4)
    return out


def validate(fields, conf):
    """Free generation (no grammar), so logprobs are honest; an off-enum label is itself a failure: conf -> 0, label coerced.
    Returns (fields, conf, problems). Raises ValueError when keys are missing (caller retries)."""
    miss = [k for k in SCHEMA["required"] if k not in fields]
    if miss:
        raise ValueError(f"missing keys {miss}")
    bad = []
    if fields["scene_type"] not in SCHEMA["properties"]["scene_type"]["enum"]:
        bad.append("scene_type")
        fields["scene_type_raw"], fields["scene_type"] = fields["scene_type"], "other"
    ok = SCHEMA["properties"]["visible_problems"]["items"]["enum"]
    if not isinstance(fields["visible_problems"], list) or any(x not in ok for x in fields["visible_problems"]):
        bad.append("visible_problems")
        fields["visible_problems"] = [x for x in fields["visible_problems"] if x in ok] or ["none"] \
            if isinstance(fields["visible_problems"], list) else ["none"]
    for k in bad:
        conf[k] = 0.0
    return fields, conf, bad


def local_classify(image_path, url="http://127.0.0.1:8081", timeout=300, retries=2):
    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    body = {"model": "vision", "temperature": 0, "max_tokens": 600, "logprobs": True, "top_logprobs": 3,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
    err = None
    for _ in range(retries + 1):
        try:
            r = _post(url + "/v1/chat/completions", body, timeout=timeout)
        except urllib.error.HTTPError as e:
            err = f"http {e.code}"
            continue
        except (urllib.error.URLError, OSError) as e:
            err = str(e)
            continue
        ch = r["choices"][0]
        content = ch["message"]["content"]
        conf = field_confidences(content, (ch.get("logprobs") or {}).get("content"))
        try:
            fields, conf, bad = validate(parse_json(content), conf)
        except (ValueError, TypeError, KeyError) as e:
            err = f"bad json: {e}"
            continue
        return {"fields": fields, "conf": conf, "off_enum": bad, "min_conf": min(conf.values()) if conf else None,
                "usage": r.get("usage"), "ms": (r.get("timings") or {}).get("predicted_ms")}
    raise RuntimeError(f"local classify failed: {err}")
