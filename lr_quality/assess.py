"""Apply a learned model to a feature dict -> assessment (probabilities, severity, fixes), flags, second opinion."""
from __future__ import annotations

import math

from . import model as M

MSG = {"blown_highlights": "blown highlights", "crushed_shadows": "crushed shadows", "underexposed": "underexposed",
       "overexposed": "overexposed", "soft_focus": "soft focus", "motion_blur": "motion blur", "blur": "soft or motion-blurred",
       "noisy": "noisy", "tilted_horizon": "tilted horizon"}


def _fix(code, sev, f, tilt):
    s = max(sev, 1)
    if code == "blown_highlights":
        p = {"Highlights": -30 * s - 10, "Whites": -15 * s}
        if f["hi_top_pct"] > 2 * max(f["hi_lower_pct"], 0.1):
            return {"code": code, "fix": "Graduated filter on the sky: Highlights down, Exposure -0.3..-0.7", "develop": p}
        return {"code": code, "fix": "Lower Highlights and Whites" + ("; may be unrecoverable (consider HDR/bracket)" if sev >= 3 else ""), "develop": p}
    if code == "crushed_shadows":
        return {"code": code, "fix": "Raise Shadows and Blacks", "develop": {"Shadows": 30 * s + 10, "Blacks": 10 * s}}
    if code in ("underexposed", "overexposed"):
        ev = math.log2(105.0 / max(f["exp_mean"], 5.0)) * 0.8
        ev = max(-2.5, min(2.5, ev))
        return {"code": code, "fix": f"Exposure {ev:+.1f} EV", "develop": {"Exposure2012": round(ev, 1)}}
    if code in ("soft_focus", "motion_blur", "blur"):
        return {"code": code, "fix": "Not fixable in Develop; reject unless the shot is irreplaceable" if sev >= 2 else "Slightly soft: Sharpening/Texture may help",
                "develop": {}}
    if code == "noisy":
        return {"code": code, "fix": "Raise Noise Reduction (Luminance)", "develop": {"LuminanceSmoothing": 20 + 15 * s}}
    if code == "tilted_horizon":
        return {"code": code, "fix": f"Straighten: rotate {'' if tilt > 0 else 'counter-'}clockwise by {abs(tilt):.1f} deg (Crop > Angle)",
                "develop": {}}
    return None


def run(model, feats: dict, image: dict, uniform_range: float = 8.0, min_long_edge: int = 256):
    """-> (assessment, flags, needs_second_opinion)."""
    f = M.derive(feats)
    tilt = M.predict_tilt(model["tilt"], f)
    f["abs_tilt_hat"] = abs(tilt)
    issues, flags, fixes, reasons = {}, [], [], []
    width = model["second_opinion"]["width"]
    for code, im in model["issues"].items():
        p, sev, ps = M.predict_issue(im, f)
        t = im["threshold"]
        noticeable = p >= t
        issues[code] = {"probability": round(p, 3), "severity": sev, "noticeable": bool(noticeable), "threshold": round(t, 3)}
        if noticeable:
            flags.append({"code": code, "severity": "bad" if sev >= 3 else "warn", "value": round(p, 3), "threshold": round(t, 3),
                          "message": f"{MSG[code]}: P(noticeable)={p:.2f}, severity {sev}/3"})
            fx = _fix(code, sev, f, tilt)
            if fx and not (code in ("soft_focus", "motion_blur") and issues.get("blur", {}).get("noticeable")):
                fixes.append(fx)
        if M.logit_dist(p, t) <= width:
            reasons.append(f"{code}: P={p:.2f} is close to its decision threshold {t:.2f} (log-odds distance <= {width:.2f})")
    # `blur` is the union of soft_focus and motion_blur; keep one blur flag and fix
    if issues.get("blur", {}).get("noticeable"):
        flags = [x for x in flags if x["code"] not in ("soft_focus", "motion_blur")]
        fixes = [x for x in fixes if x["code"] not in ("soft_focus", "motion_blur")]
    if max(image["width"], image["height"]) < min_long_edge:
        reasons.append("very small image")
    if f["exp_range"] < uniform_range:
        reasons.append("almost uniform image")
    flags.sort(key=lambda x: (x["severity"] != "bad", -x["value"]))
    assessment = {"model": model.get("version", "unversioned"), "issues": issues,
                  "tilt": {"rotate_cw_deg": round(tilt, 2)}, "fixes": fixes}
    return assessment, flags, {"value": bool(reasons), "reasons": list(dict.fromkeys(reasons))}
