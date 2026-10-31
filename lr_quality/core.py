"""Histogram / exposure / focus / tilt / noise metrics. Pure functions; numpy + Pillow (+ optional cv2)."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:  # optional accelerator; results match the numpy path to float tolerance
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None

SCHEMA = "lr_quality/1"
SCENES = ("landscape", "city", "people", "sunset", "snow", "interior", "night")
_PCTS = (1, 5, 25, 50, 75, 95, 99)


def default_thresholds() -> dict:
    return json.loads((Path(__file__).with_name("thresholds.json")).read_text("utf-8"))


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _load(image):
    """-> (RGB PIL image, format or None)."""
    if isinstance(image, Image.Image):
        img, fmt = image, image.format
    elif isinstance(image, (bytes, bytearray, memoryview)):
        img = Image.open(io.BytesIO(bytes(image)))
        fmt = img.format
    elif isinstance(image, (str, os.PathLike)):
        img = Image.open(image)
        fmt = img.format
    else:
        raise TypeError(f"unsupported image type {type(image).__name__}")
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img.convert("RGB"), fmt


def _filters(g, use_cv2):
    """Interior 3x3 Laplacian and Immerkaer noise-kernel responses of float32 luma."""
    if use_cv2:
        kl = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], np.float32)
        kn = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
        b = cv2.BORDER_REFLECT_101
        return (cv2.filter2D(g, -1, kl, borderType=b)[1:-1, 1:-1], cv2.filter2D(g, -1, kn, borderType=b)[1:-1, 1:-1])
    c = g[1:-1, 1:-1]
    ud = g[:-2, 1:-1] + g[2:, 1:-1]
    lr = g[1:-1, :-2] + g[1:-1, 2:]
    lap = ud + lr - 4 * c
    corners = g[:-2, :-2] + g[:-2, 2:] + g[2:, :-2] + g[2:, 2:]
    return lap, corners - 2 * (ud + lr) + 4 * c


def _tiles(a, ty, tx):
    th, tw = a.shape[0] // ty, a.shape[1] // tx
    return a[: th * ty, : tw * tx].reshape(ty, th, tx, tw)


def _hist_pct(cdf, n, p):
    return int(np.searchsorted(cdf, p / 100.0 * n, side="left"))


def _exposure(rgb, q, th):
    n = q.size
    hist = np.bincount(q.ravel(), minlength=256)
    cdf = np.cumsum(hist)
    c = th["clip"]
    hi, lo = c["high_level"], c["low_level"]
    h3 = q.shape[0] // 3
    out = {"mean_luma": float((hist * np.arange(256)).sum() / n)}
    for p in _PCTS:
        out[f"p{p:02d}"] = _hist_pct(cdf, n, p)
    out["clip_high_pct"] = float(hist[hi:].sum() * 100.0 / n)
    out["clip_low_pct"] = float(hist[: lo + 1].sum() * 100.0 / n)
    out["clip_high_pct_by_channel"] = {k: float((rgb[..., i] >= hi).mean() * 100.0) for i, k in enumerate("rgb")}
    out["upper_third_clip_high_pct"] = float((q[:h3] >= hi).mean() * 100.0)
    out["midtone_mass"] = float(hist[64:193].sum() / n)
    out["histogram_luma"] = (hist.reshape(32, 8).sum(1) / n).tolist()
    out["dynamic_range_p99_p01"] = out["p99"] - out["p01"]
    out["vs_targets"] = None
    out["batch_percentile"] = None
    return out


def _focus_noise(g, th, use_cv2):
    f = th["focus"]
    lap, imm = _filters(g, use_cv2)
    ty = max(1, min(f["tiles"], lap.shape[0] // 4))
    tx = max(1, min(f["tiles"], lap.shape[1] // 4))
    lt = _tiles(lap, ty, tx)
    tv = np.maximum((lt * lt).mean((1, 3)) - lt.mean((1, 3)) ** 2, 0).ravel()
    gv = float(lap.var())
    p90 = float(np.percentile(tv, 90))
    focus = {
        "score_tile_p90": p90,
        "score_global": gv,
        "sharp_tile_fraction": float((tv >= f["sharp_tile_var"]).mean()),
        "method": "laplacian_variance_tiles",
        "relative_to_batch": None,
    }
    # Immerkaer sigma on the flattest quartile of non-saturated tiles
    gt = _tiles(g[1:-1, 1:-1], ty, tx).mean((1, 3)).ravel()
    ok = np.flatnonzero((gt < 250) & (gt > 5))
    sigma = None
    if ok.size:
        take = ok[np.argsort(tv[ok], kind="stable")[: max(1, ok.size // 4)]]
        ia = _tiles(np.abs(imm), ty, tx).mean((1, 3)).ravel()
        sigma = float(np.sqrt(np.pi / 2) / 6.0 * ia[take].mean())
    return focus, {"sigma_estimate": sigma}


def _tilt(img, th):
    """Hough over near-horizontal edges in the upper-middle band. angle>0: horizon descends to the right."""
    t = th["tilt"]
    gi = img.convert("L")
    s = 800.0 / max(gi.size)
    if s < 1:
        gi = gi.resize((max(8, round(gi.width * s)), max(8, round(gi.height * s))), Image.BILINEAR)
    a = np.asarray(gi, np.float32)
    H, W = a.shape
    if H < 16 or W < 16:
        return None
    gx = (a[:-2, 2:] + 2 * a[1:-1, 2:] + a[2:, 2:]) - (a[:-2, :-2] + 2 * a[1:-1, :-2] + a[2:, :-2])
    gy = (a[2:, :-2] + 2 * a[2:, 1:-1] + a[2:, 2:]) - (a[:-2, :-2] + 2 * a[:-2, 1:-1] + a[:-2, 2:])
    y0, y1 = int(0.15 * H), int(0.70 * H)
    gx, gy = gx[y0:y1], gy[y0:y1]
    mag = np.hypot(gx, gy)
    ys, xs = np.nonzero((mag >= 40) & (np.abs(gx) <= 0.21 * np.abs(gy)))
    if ys.size < 20:
        return None
    if ys.size > 8000:
        top = np.argpartition(-mag[ys, xs], 8000)[:8000]
        ys, xs = ys[top], xs[top]
    ys = (ys + y0 + 1 - H / 2.0).astype(np.float32)
    xs = (xs + 1 - W / 2.0).astype(np.float32)
    angs = np.arange(-10.0, 10.0001, 0.2)
    ar = np.radians(angs).astype(np.float32)
    rho = ys[:, None] * np.cos(ar)[None] - xs[:, None] * np.sin(ar)[None]
    bw = 4.0
    off = float(-rho.min())
    rb = ((rho + off) / bw).astype(np.int64)
    nb = int(rb.max()) + 1
    acc = np.bincount((np.arange(len(angs))[None] * nb + rb).ravel(), minlength=len(angs) * nb).reshape(len(angs), nb)
    acc = acc.astype(np.int64)
    work = acc.copy()
    lines = []
    top_votes = None
    for _ in range(6):
        ai, ri = np.unravel_index(int(work.argmax()), work.shape)
        v = int(work[ai, ri])
        if top_votes is None:
            top_votes = v
        if v < t["min_votes_frac"] * W or v < 0.25 * top_votes:
            break
        lo, hi = max(0, ai - 5), min(len(angs), ai + 6)
        prof = acc[lo:hi, max(0, ri - 1): ri + 2].sum(1).astype(float)
        m = prof >= 0.7 * prof.max()
        ang = float((angs[lo:hi][m] * prof[m]).sum() / prof[m].sum())
        lines.append((ang, v))
        work[max(0, ai - 15): ai + 16, max(0, ri - 4): ri + 5] = 0
    if not lines:
        return None
    best = max(lines, key=lambda x: x[1])
    agree = [ln for ln in lines if abs(ln[0] - best[0]) <= t["agree_deg"]]
    tot = sum(v for _, v in lines)
    av = sum(v for _, v in agree)
    angle = sum(a_ * v for a_, v in agree) / av
    return {"angle_deg": float(angle), "confidence": float(min(1.0, len(agree) / 3.0) * av / tot), "lines_used": len(agree)}


def _flags(res, th, scene_hint):
    """-> (flags, needs_second_opinion) from the metric dicts in res. Re-run after batch fields are set."""
    e, f, n, tl, im = res["exposure"], res["focus"], res["noise"], res["tilt"], res["image"]
    c, lu, fo, nz, ti, so = th["clip"], th["luma"], th["focus"], th["noise"], th["tilt"], th["second_opinion"]
    mean_frac = e["mean_luma"] / 255.0
    rel = f["relative_to_batch"]
    rules = [  # code, value, (warn, bad), direction, what
        ("blown_highlights", e["clip_high_pct"], (c["high_warn"], c["high_bad"]), "gt", f"% of pixels with luma >= {c['high_level']}"),
        ("crushed_shadows", e["clip_low_pct"], (c["low_warn"], c["low_bad"]), "gt", f"% of pixels with luma <= {c['low_level']}"),
        ("underexposed", mean_frac, (lu["under_warn"], lu["under_bad"]), "lt", "mean luma / 255"),
        ("overexposed", mean_frac, (lu["over_warn"], lu["over_bad"]), "gt", "mean luma / 255"),
    ]
    if rel is not None:
        rules.append(("soft_focus", rel, (fo["rel_warn"], fo["rel_bad"]), "lt", "tile-p90 sharpness / batch median"))
    else:
        rules.append(("soft_focus", f["score_tile_p90"], (fo["abs_warn"], fo["abs_bad"]), "lt", "tile-p90 Laplacian variance (absolute, domain dependent)"))
    if n["sigma_estimate"] is not None:
        rules.append(("noisy", n["sigma_estimate"], (nz["warn"], nz["bad"]), "gt", "estimated noise sigma (0-255 scale)"))
    flags, reasons = [], []
    fired = set()
    for code, v, (w, b), d, what in rules:
        hit = lambda x: v > x if d == "gt" else v < x  # noqa: E731
        sev = "bad" if hit(b) else "warn" if hit(w) else None
        if sev:
            fired.add(code)
            flags.append({"code": code, "severity": sev, "value": round(v, 4), "threshold": b if sev == "bad" else w,
                          "message": f"{code.replace('_', ' ')}: {what} = {v:.3g} ({'above' if d == 'gt' else 'below'} {b if sev == 'bad' else w})"})
        near = abs(v - w) <= so["margin"] * abs(w)
        if near:
            reasons.append(f"{code}: {what} = {v:.3g} within {int(so['margin'] * 100)}% of threshold {w}")
        if code == "soft_focus" and rel is None and sev:
            reasons.append("soft_focus: absolute blur threshold is domain dependent; no batch to compare with")
        if scene_hint and (sev or near) and code in so["scene_unreliable"].get(scene_hint, []):
            reasons.append(f"scene_hint '{scene_hint}' makes the {code} rule unreliable")
    # tilt
    if tl is not None:
        a = abs(tl["angle_deg"])
        if ti["min_deg"] <= a <= ti["max_deg"]:
            if tl["lines_used"] >= ti["min_lines"]:
                flags.append({"code": "tilted_horizon", "severity": "warn", "value": round(tl["angle_deg"], 2),
                              "threshold": ti["min_deg"], "message": f"tilted horizon: {tl['angle_deg']:.2f} deg from {tl['lines_used']} agreeing lines"})
            else:
                reasons.append("tilt: possible horizon tilt seen from too few lines")
        for bound in (ti["min_deg"], ti["max_deg"]):
            if abs(a - bound) <= so["margin"] * bound:
                reasons.append(f"tilt: |angle| {a:.2f} deg within {int(so['margin'] * 100)}% of bound {bound}")
    # conflicts
    if f["score_tile_p90"] > fo["abs_warn"] and f["score_global"] < fo["conflict_ratio"] * f["score_tile_p90"]:
        reasons.append("focus signals conflict: tile sharpness high but global sharpness low (small sharp area)")
    if "blown_highlights" in fired and ("crushed_shadows" in fired or "underexposed" in fired):
        reasons.append("exposure signals conflict: highlights clipped while shadows/overall are dark")
    if "blown_highlights" in fired and e["upper_third_clip_high_pct"] >= 2 * e["clip_high_pct"] > 0:
        reasons.append("clipping is concentrated in the upper third (sky/sun?)")
    # unusual images
    if max(im["width"], im["height"]) < so["min_long_edge"]:
        reasons.append("very small image")
    if e["dynamic_range_p99_p01"] < so["uniform_range"]:
        reasons.append("almost uniform image")
    order = {"info": 0, "warn": 1, "bad": 2}
    flags.sort(key=lambda x: -order[x["severity"]])
    return flags, {"value": bool(reasons), "reasons": list(dict.fromkeys(reasons))}


def _vs_targets(e, targets, scene_hint):
    if not targets:
        return None
    sc = targets.get("scenes", targets)
    name = scene_hint if scene_hint in sc else "default" if "default" in sc else None
    if name is None:
        return None
    t = sc[name]
    keys = ["mean_luma"] + [f"p{p:02d}" for p in _PCTS]
    return {"scene": name, "delta": {k: round(e[k] - t[k], 3) for k in keys if k in t}}


def _round(o):
    if isinstance(o, float):
        return round(o, 4)
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v) for v in o]
    return o


def analyze(image, *, scene_hint=None, thresholds=None, targets=None, long_edge=1600, accel="auto") -> dict:
    """Analyse one image (bytes | path | PIL image). accel: 'auto' (cv2 if installed) or 'numpy'."""
    return finalize(analyze_raw(image, scene_hint=scene_hint, thresholds=thresholds, targets=targets,
                                long_edge=long_edge, accel=accel))


def analyze_raw(image, *, scene_hint=None, thresholds=None, targets=None, long_edge=1600, accel="auto") -> dict:
    """Like analyze() but keeps the private `_th` key so batch_pass can re-evaluate flags."""
    if scene_hint is not None and scene_hint not in SCENES:
        raise ValueError(f"scene_hint must be one of {SCENES}")
    th = _merge(default_thresholds(), copy.deepcopy(thresholds or {}))
    img, fmt = _load(image)
    w0, h0 = img.size
    if min(w0, h0) < 8:
        raise ValueError("image too small to analyse (<8 px)")
    s = long_edge / max(w0, h0)
    if s < 1:
        img = img.resize((max(8, round(w0 * s)), max(8, round(h0 * s))), Image.LANCZOS)
    rgb = np.asarray(img)
    q = np.asarray(img.convert("L"))  # Pillow integer Rec.601 luma: fast, platform-deterministic
    g = q.astype(np.float32)
    use_cv2 = cv2 is not None and accel == "auto"
    exp = _exposure(rgb, q, th)
    exp["vs_targets"] = _vs_targets(exp, targets, scene_hint)
    focus, noise = _focus_noise(g, th, use_cv2)
    res = {
        "schema": SCHEMA,
        "source": None,
        "scene_hint": scene_hint,
        "image": {"width": w0, "height": h0, "analysed_long_edge": max(img.size), "format": fmt},
        "exposure": exp,
        "focus": focus,
        "tilt": _tilt(img, th),
        "noise": noise,
    }
    res["flags"], res["needs_second_opinion"] = _flags(res, th, scene_hint)
    res["_th"] = th  # stripped by finalize(); kept so the batch pass can re-evaluate
    return res


def finalize(res: dict) -> dict:
    return _round({k: v for k, v in res.items() if k != "_th"})


def batch_pass(results: list) -> None:
    """In place: fill batch-relative fields on the successful results (dicts from analyze) and re-evaluate flags."""
    ok = [r for r in results if "error" not in r]
    if not ok:
        return
    th = ok[0]["_th"]
    scores = np.array([r["focus"]["score_tile_p90"] for r in ok])
    means = np.array([r["exposure"]["mean_luma"] for r in ok])
    med = float(np.median(scores))
    big = len(ok) >= th["focus"]["min_batch"]
    for r in ok:
        if big and med > 0:
            r["focus"]["relative_to_batch"] = r["focus"]["score_tile_p90"] / med
        m = r["exposure"]["mean_luma"]
        r["exposure"]["batch_percentile"] = float(100.0 * ((means < m).sum() + 0.5 * (means == m).sum()) / len(ok))
        r["flags"], r["needs_second_opinion"] = _flags(r, r["_th"], r["scene_hint"])
