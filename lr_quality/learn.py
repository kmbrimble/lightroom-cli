"""Training / evaluation orchestration: reviews + feature cache -> model.json, metrics tables."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import labels as L
from . import model as M

V1_CODE = {"blown_highlights": "blown_highlights", "crushed_shadows": "crushed_shadows", "underexposed": "underexposed",
           "overexposed": "overexposed", "soft_focus": "soft_focus", "blur": "soft_focus", "noisy": "noisy",
           "tilted_horizon": "tilted_horizon"}
MAX_SECOND_OPINION = 0.15


def load_cache(path):
    out = {}
    for line in Path(path).read_text("utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            if "result" in d:
                out[d["id"]] = d["result"]
    return out


def join(recs, cache):
    keep = [r for r in recs if r["id"] in cache]
    return keep, [M.derive(cache[r["id"]]["features"]) for r in keep], [cache[r["id"]] for r in keep]


def split(recs):
    ho = np.array([L.is_heldout(r["id"]) for r in recs])
    return ~ho, ho


def fit_all(F, recs, seed=7):
    """Fit tilt + all issue heads on (F, recs). -> (model dict, oof P(>=2) matrix [n, issues], used-mask matrix)."""
    tm = M.fit_tilt(F, recs)
    M.add_tilt_hat(F, tm)
    issues, oof, used = {}, {}, {}
    for code in M.FEATURES:
        im, p, u = M.train_issue(F, recs, code, seed=seed)
        if im:
            issues[code], oof[code], used[code] = im, p, u
    model = {"schema": M.MODEL_SCHEMA, "issues": issues, "tilt": tm, "second_opinion": {"width": 0.15, "max_rate": MAX_SECOND_OPINION},
             "n_reviews": len(recs), "n_weak": sum(r["source"] == "weak" for r in recs)}
    real = np.array([r["source"] != "weak" for r in recs])
    model["second_opinion"]["width"] = pick_width(model, oof, used, real)
    return model, oof, used


def _ambiguous(model, P, width):
    """P: {code: array}. A photo is ambiguous when any issue's probability is within `width` of its operating threshold."""
    amb = np.zeros(len(next(iter(P.values()))), bool)
    for code, p in P.items():
        t = model["issues"][code]["threshold"]
        amb |= np.nan_to_num(np.abs(p - t) <= width, nan=False)
    return amb


def pick_width(model, oof, used, real):
    lo, hi = 0.0, 0.5
    for _ in range(25):
        mid = (lo + hi) / 2
        rate = _ambiguous(model, oof, mid)[real].mean() if real.any() else 0
        lo, hi = (mid, hi) if rate <= MAX_SECOND_OPINION else (lo, mid)
    return round(lo, 4)


def predict_matrix(model, F):
    M.add_tilt_hat(F, model["tilt"])
    P, S = {}, {}
    for code, im in model["issues"].items():
        pr = [M.predict_issue(im, f) for f in F]
        P[code] = np.array([x[0] for x in pr])
        S[code] = np.array([x[1] for x in pr])
    return P, S


def _group(recs, key):
    return [str(r.get(key) or "?") for r in recs]


def evaluate(model, F, recs, results=None, groups=("kind", "scene")):
    """Metrics of `model` on (F, recs) (non-weak rows only). results = analyze() outputs, for the v1-flag baseline."""
    keep = [i for i, r in enumerate(recs) if r["source"] != "weak"]
    recs = [recs[i] for i in keep]
    F = [F[i] for i in keep]
    results = [results[i] for i in keep] if results else None
    P, S = predict_matrix(model, F)
    out = {"n": len(recs), "issues": {}, "groups": {}}
    for code, im in model["issues"].items():
        t = im["threshold"]
        row = {"threshold": t}
        for k in (1, 2, 3):
            y = M._label_vec(recs, code, k)
            u = y >= 0
            if u.sum() == 0 or y[u].sum() == 0:
                continue
            pred = (P[code] >= t)[u] if k == 2 else (S[code] >= k)[u]
            cell = {"auc": M.auc(P[code][u], y[u]), **M.prf(pred, y[u])}
            if results is not None and code in V1_CODE:
                v1 = np.array([any(f["code"] == V1_CODE[code] for f in r["flags"]) for r in results])
                cell["v1"] = M.prf(v1[u], y[u])
            row[f"ge{k}"] = cell
        out["issues"][code] = row
        y2 = M._label_vec(recs, code, 2)
        for g in groups:
            gv = _group(recs, g)
            for v in sorted(set(gv)):
                m = np.array([x == v for x in gv]) & (y2 >= 0)
                if m.sum() >= 8:
                    out["groups"].setdefault(g, {}).setdefault(code, {})[v] = M.prf((P[code] >= t)[m], y2[m] == 1)
    # second opinion
    amb = _ambiguous(model, P, model["second_opinion"]["width"])
    err = np.zeros(len(recs), bool)
    for code, im in model["issues"].items():
        y2 = M._label_vec(recs, code, 2)
        err |= (y2 >= 0) & ((P[code] >= im["threshold"]) != (y2 == 1))
    out["second_opinion"] = {"rate": float(amb.mean()), "width": model["second_opinion"]["width"], "n_photos_with_error": int(err.sum()),
                             "errors_in_flagged": float((err & amb).sum() / err.sum()) if err.any() else None,
                             "error_rate_flagged": float(err[amb].mean()) if amb.any() else None,
                             "error_rate_unflagged": float(err[~amb].mean()) if (~amb).any() else None}
    # tilt MAE (photos with a trustworthy reference)
    tl = [i for i, r in enumerate(recs) if r["tilt_deg"] is not None and r["tilt_conf"] >= 0.6]
    if tl:
        hat = np.array([M.predict_tilt(model["tilt"], F[i]) for i in tl])
        hv1 = np.array([F[i]["hough_angle"] if F[i]["hough_valid"] else 0.0 for i in tl])
        tru = np.array([recs[i]["tilt_deg"] for i in tl])
        out["tilt"] = {"n": len(tl), "mae_model": float(np.abs(hat - tru).mean()), "mae_v1_hough": float(np.abs(hv1 - tru).mean()),
                       "mae_zero": float(np.abs(tru).mean()), "mae_model_abs": float(np.abs(np.abs(hat) - np.abs(tru)).mean()),
                       "sign_agree": float((np.sign(hat) == np.sign(tru)).mean())}
    return out


def metric_summary(ev):
    """Scalars used for ship/no-ship comparison: mean AUC and mean F1 at ge2 over issues with data."""
    a = [c["ge2"]["auc"] for c in ev["issues"].values() if "ge2" in c and c["ge2"]["auc"] is not None]
    f = [c["ge2"]["f1"] or 0 for c in ev["issues"].values() if "ge2" in c]
    return {"mean_auc": float(np.mean(a)) if a else 0.0, "mean_f1": float(np.mean(f)) if f else 0.0}


def fmt_table(ev, title=""):
    L_ = [title, f"{'issue':18}{'thr':>6}{'n+':>5}{'AUC':>7}{'P':>7}{'R':>7}{'F1':>7} | v1 P / R"]
    num = lambda x: "  -  " if x is None else f"{x:5.2f}"  # noqa: E731
    for code, row in ev["issues"].items():
        for k in (1, 2, 3):
            c = row.get(f"ge{k}")
            if not c:
                continue
            v = c.get("v1")
            L_.append(f"{code + ' >=' + str(k):18}{row['threshold']:6.2f}{c['n_pos']:5d}  {num(c['auc'])} {num(c['precision'])} {num(c['recall'])} {num(c['f1'])}"
                      + (f" | {num(v['precision'])} / {num(v['recall'])}" if v else ""))
    so = ev["second_opinion"]
    L_.append(f"second opinion: rate {so['rate']:.1%} (width {so['width']}); photos with >=1 error {so['n_photos_with_error']}, "
              f"of which flagged {so['errors_in_flagged'] if so['errors_in_flagged'] is None else format(so['errors_in_flagged'], '.0%')}")
    if "tilt" in ev:
        t = ev["tilt"]
        L_.append(f"tilt (n={t['n']}): MAE model {t['mae_model']:.2f} deg, v1 hough {t['mae_v1_hough']:.2f}, predict-zero {t['mae_zero']:.2f}; sign agreement {t['sign_agree']:.0%}")
    return "\n".join(L_)


def label_noise_bound(repeat_pairs):
    """Agreement between two reviews of the same photo -> per-issue precision/recall ceiling (one review as 'truth')."""
    out = {}
    for code in M.FEATURES:
        a = np.array([x["targets"].get(code) for x, _ in repeat_pairs], dtype=object)
        b = np.array([y["targets"].get(code) for _, y in repeat_pairs], dtype=object)
        ok = [(i, j) for i, j in zip(a, b) if i is not None and j is not None]
        if len(ok) >= 8:
            ya, yb = np.array([int(i >= 2) for i, _ in ok]), np.array([int(j >= 2) for _, j in ok])
            out[code] = {**M.prf(ya.astype(bool), yb.astype(bool)), "exact_agree": float(np.mean([i == j for i, j in ok]))}
    return out


def cv_report(model, oof, recs):
    """Out-of-fold metrics on the dev rows (threshold was chosen on these same OOF scores: slightly optimistic)."""
    out = {}
    for code, p in oof.items():
        y = M._label_vec(recs, code, 2)
        real = np.array([r["source"] != "weak" for r in recs])
        u = (y >= 0) & real & ~np.isnan(p)
        if u.sum() and y[u].sum():
            out[code] = {"auc": M.auc(p[u], y[u]), **M.prf(p[u] >= model["issues"][code]["threshold"], y[u]),
                         "threshold": model["issues"][code]["threshold"]}
    return out


def fmt_cv(cv):
    num = lambda x: "  -  " if x is None else f"{x:5.2f}"  # noqa: E731
    return "\n".join(f"{c:18}{r['threshold']:6.2f}{r['n_pos']:5d}  {num(r['auc'])} {num(r['precision'])} {num(r['recall'])} {num(r['f1'])}"
                     for c, r in cv.items())


def apply_meta(recs, meta_path):
    if not meta_path:
        return
    meta = {str(p["id"]): p for p in json.loads(Path(meta_path).read_text("utf-8")).get("photos", [])}
    for r in recs:
        m = meta.get(r["id"])
        if m:
            r["kind"] = r["kind"] or m.get("kind")
            r["scene"] = r["scene"] or m.get("scene_type")
