"""Review/label ingestion: normalise any review source to {id, targets{issue: 0..3|None}, tilt_deg, tilt_conf, weight, kind, scene}.

Sources: Sonnet-style rubric NDJSON (highlights/shadows/exposure/focus/motion_blur/noise/horizon_tilt_deg/tilt_confidence),
`weak` triage final.ndjson (Claude visible_problems; low weight), and a user accept/reject adapter (`verdict`).
Later records for the same id replace earlier ones. Nothing here knows file names or paths; ids only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ISSUES = ("blown_highlights", "crushed_shadows", "underexposed", "overexposed", "soft_focus", "motion_blur", "blur", "noisy",
          "tilted_horizon")
TILT_MIN_DEG = 1.0      # |tilt| >= this is "noticeable" (label severity >= 2)
TILT_CONF_MIN = 0.6     # below this the reviewer saw no reliable level reference
WEAK_WEIGHT = 0.3


def tilt_severity(deg):
    a = abs(deg)
    return 0 if a < 0.5 else 1 if a < TILT_MIN_DEG else 2 if a < 2.5 else 3


def _int(v):
    try:
        return None if v is None else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def from_rubric(d: dict, weight=1.0) -> dict | None:
    """One Sonnet-rubric record (flat, or nested under 'labels'/'fields')."""
    r = d.get("labels") or d.get("fields") or d
    pid = d.get("id") or d.get("photo_id")
    if pid is None:
        return None
    hi, sh, ex = _int(r.get("highlights")), _int(r.get("shadows")), _int(r.get("exposure"))
    fo, mb, nz = _int(r.get("focus")), _int(r.get("motion_blur")), _int(r.get("noise"))
    deg, conf = r.get("horizon_tilt_deg"), r.get("tilt_confidence") or 0
    t = {"blown_highlights": hi, "crushed_shadows": sh,
         "underexposed": None if ex is None else max(0, -ex), "overexposed": None if ex is None else max(0, ex),
         "soft_focus": fo, "motion_blur": mb, "noisy": nz,
         "blur": None if fo is None or mb is None else max(fo, mb)}
    good = deg is not None and conf >= TILT_CONF_MIN
    t["tilted_horizon"] = tilt_severity(deg) if good else 0
    return {"id": str(pid), "targets": t, "tilt_deg": float(deg) if good else None, "tilt_conf": float(conf),
            "weight": float(d.get("weight", weight)), "kind": d.get("kind"), "scene": d.get("scene_type"),
            "uncertain": bool(d.get("uncertain")), "source": d.get("source", "rubric"), "keeper": d.get("keeper_score")}


def from_weak(d: dict, weight=WEAK_WEIGHT) -> dict | None:
    """triage final.ndjson record: Claude's visible_problems (positives are informative, absence only for covered issues)."""
    cf = (d.get("claude") or {}).get("fields")  # only escalated photos carry a Claude opinion
    if "photo_id" not in d or not cf:
        return None
    vp = set(cf.get("visible_problems") or [])
    pos = lambda *k: 2 if vp & set(k) else 0  # noqa: E731
    t = {i: None for i in ISSUES}
    t.update(blown_highlights=pos("blown_highlights"), crushed_shadows=pos("too_dark"), underexposed=pos("too_dark"),
             soft_focus=pos("blurry"), motion_blur=pos("motion_blur"), blur=pos("blurry", "motion_blur"),
             tilted_horizon=pos("tilted_horizon"))
    return {"id": str(d["photo_id"]), "targets": t, "tilt_deg": None, "tilt_conf": 0.0, "weight": weight, "kind": None,
            "scene": cf.get("scene_type"), "uncertain": False, "source": "weak", "keeper": None}


def from_verdict(d: dict, weight=1.0) -> dict | None:
    """User accept/reject: {"id", "verdict": "accept"|"reject", "issues": [codes]}. Reject+issues => those issues sev 2."""
    if "verdict" not in d:
        return None
    bad = set(d.get("issues") or [])
    t = {i: (2 if i in bad else None) for i in ISSUES}
    if d["verdict"] == "accept":
        t = {i: 0 for i in ISSUES if i != "tilted_horizon"} | {"tilted_horizon": 0}
    return {"id": str(d["id"]), "targets": t, "tilt_deg": None, "tilt_conf": 0.0, "weight": float(d.get("weight", weight)),
            "kind": d.get("kind"), "scene": d.get("scene_type"), "uncertain": False, "source": "user", "keeper": None}


def load(paths, weak=()):
    """-> list of normalised records, de-duplicated by id (rubric/user beat weak; later beats earlier)."""
    by = {}
    for p, adapter, w in [(p, None, 1.0) for p in paths] + [(p, from_weak, WEAK_WEIGHT) for p in weak]:
        for line in Path(p).read_text("utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            r = (adapter or (from_verdict if "verdict" in d else from_rubric))(d, w)
            if r is None:
                continue
            old = by.get(r["id"])
            if old and old["source"] != "weak" and r["source"] == "weak":
                continue
            by[r["id"]] = r
    return list(by.values())


def is_heldout(pid: str, frac=0.25, seed="lrq2") -> bool:
    """Fixed, stable split: depends only on (seed, id), so adding labels never moves an existing photo across it."""
    h = int(hashlib.sha1(f"{seed}:{pid}".encode()).hexdigest()[:8], 16)
    return (h % 10_000) < frac * 10_000
