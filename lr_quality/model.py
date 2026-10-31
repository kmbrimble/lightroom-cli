"""Small interpretable models (numpy only): L2 logistic regression + Platt calibration, ridge for tilt, metrics, CV."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import labels as L

MODEL_SCHEMA = "lr_quality_model/1"
DEFAULT_MODEL = Path(__file__).with_name("model.json")

FEATURES = {
    "blown_highlights": ["hi_pct", "hi_big_pct", "hi_small_pct", "hi_interior_pct", "hi_max_blob_pct", "hi_lower_pct", "hi_top_pct",
                         "hi_pct_240", "hi_anych_pct", "exp_p95", "exp_p99", "exp_mean"],
    "crushed_shadows": ["lo_pct", "lo_pct_16", "lo_pct_30", "lo_interior_pct", "exp_p01", "exp_p05", "exp_p25", "exp_mean", "exp_range"],
    "underexposed": ["exp_mean", "exp_log_mean", "exp_p50", "exp_p75", "exp_p95", "exp_center_mean", "exp_lower_mean", "lo_pct_30"],
    "overexposed": ["exp_mean", "exp_log_mean", "exp_p50", "exp_p25", "exp_p05", "exp_center_mean", "hi_pct_240", "hi_big_pct"],
    "soft_focus": ["foc_lv_max", "foc_lv_p90", "foc_lv_p75", "foc_lv_med", "foc_top_ratio", "foc_ratio_min", "foc_acutance",
                   "foc_edge_p99", "foc_top_e1", "foc_tex_frac", "foc_flat_frac", "foc_near_best_frac", "noise_sigma"],
    "motion_blur": ["foc_top_ratio", "foc_top_ratio_x", "foc_top_ratio_y", "foc_ratio_min", "foc_ratio_aniso", "foc_acutance",
                    "foc_lv_p90", "foc_lv_max", "foc_edge_p99", "foc_top_e1", "foc_near_best_frac"],
    "noisy": ["noise_sigma", "exp_mean", "exp_p25", "foc_lv_med", "lo_pct_30", "foc_flat_frac"],
    "tilted_horizon": ["abs_hough", "hough_conf", "hough_lines", "abs_ori_h", "ori_h_share", "ori_h_massfrac", "ori_h_level",
                       "abs_ori_v", "ori_v_share", "ori_v_massfrac", "hv_disagree", "tilt_gate", "tilt_gate_amt", "tilt_gate_strong", "abs_st_h", "st_h_share", "st_h_mass", "abs_st_v", "st_v_share", "st_v_mass", "foc_flat_frac", "abs_tilt_hat"],
}
FEATURES["blur"] = sorted(set(FEATURES["soft_focus"]) | set(FEATURES["motion_blur"]))
TILT_REG = ["hough_w", "st_h_w", "st_h_g", "st_v_w", "ori_h_w", "ori_v_w"]
_LOG = ("_pct", "foc_lv", "foc_edge", "foc_top_e1", "foc_global", "noise_sigma", "hi_blobs")


def derive(f: dict) -> dict:
    """Add derived features (|angles|, shares-weighted angles) used by the tilt models."""
    g = dict(f)
    g["hough_w"] = f["hough_angle"] * f["hough_conf"]
    g["ori_h_w"] = f["ori_h_peak"] * f["ori_h_share"]
    g["ori_v_w"] = f["ori_v_peak"] * f["ori_v_share"]
    g["abs_hough"] = abs(f["hough_angle"]) * f["hough_valid"]
    g["abs_ori_h"] = abs(f["ori_h_peak"])
    g["abs_ori_v"] = abs(f["ori_v_peak"])
    g["st_h_w"] = f["st_h_peak"] * f["st_h_share"]
    g["st_v_w"] = f["st_v_peak"] * f["st_v_share"]
    g["st_h_g"] = f["st_h_peak"] * float(f["st_h_share"] > 0.5)
    ev = float(f["st_h_share"] > 0.4 and f["st_h_mass"] > 0.05)
    g["tilt_gate"] = ev
    g["tilt_gate_amt"] = ev * min(abs(f["st_h_peak"]), 6.0)
    g["tilt_gate_strong"] = float(f["st_h_share"] > 0.5 and f["st_h_mass"] > 0.1) * min(abs(f["st_h_peak"]), 6.0)
    g["abs_st_h"] = abs(f["st_h_peak"])
    g["abs_st_v"] = abs(f["st_v_peak"])
    g["hv_disagree"] = abs(f["ori_h_peak"] - f["ori_v_peak"])
    return g


def _tf(name, v):
    return np.log1p(max(v, 0.0)) if any(k in name for k in _LOG) else v


def vec(f: dict, names) -> np.ndarray:
    return np.array([_tf(n, f[n]) for n in names], np.float64)


# ---------------------------------------------------------------- fitting
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logit(X, y, w, lam):
    """Weighted L2 logistic regression by Newton/IRLS on standardised X. -> (mu, sd, coef, bias)."""
    mu = np.average(X, axis=0, weights=w)
    sd = np.sqrt(np.average((X - mu) ** 2, axis=0, weights=w)) + 1e-9
    Z = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    beta = np.zeros(Z.shape[1])
    R = np.eye(Z.shape[1]) * lam
    R[-1, -1] = 0
    for _ in range(40):
        p = _sigmoid(Z @ beta)
        g = Z.T @ (w * (p - y)) + R @ beta
        H = (Z * (w * p * (1 - p) + 1e-9)[:, None]).T @ Z + R + 1e-6 * np.eye(len(beta))
        step = np.linalg.solve(H, g)
        beta -= step
        if np.abs(step).max() < 1e-7:
            break
    return mu, sd, beta[:-1], float(beta[-1])


def decision(m, X):
    return ((X - m[0]) / m[1]) @ m[2] + m[3]


def fit_platt(s, y, w):
    """P = sigmoid(a*s + b) on out-of-fold decision values."""
    _, _, beta, b = fit_logit(s[:, None], y, w, 1e-3)
    sd = np.sqrt(np.average((s - np.average(s, weights=w)) ** 2, weights=w)) + 1e-9
    return float(beta[0] / sd), float(b - beta[0] * np.average(s, weights=w) / sd)


def logit_dist(p, t):
    """|logit(p) - logit(t)|: distance to the decision threshold in log-odds (rare positives sit far below t in probability)."""
    lg = lambda x: np.log(np.clip(x, 1e-4, 1 - 1e-4) / (1 - np.clip(x, 1e-4, 1 - 1e-4)))  # noqa: E731
    return np.abs(lg(p) - lg(t))


def folds(groups, key, k, seed):
    """Stratified, grouped k-fold: rows sharing a group (a photo and its rotated variants) always land in the same fold."""
    rng = np.random.default_rng(seed)
    gk = {}
    for g, v in zip(groups, key):
        gk[g] = max(gk.get(g, 0), v)
    gf = {}
    for v in sorted(set(gk.values())):
        gs = sorted(g for g, x in gk.items() if x == v)
        rng.shuffle(gs)
        for i, g in enumerate(gs):
            gf[g] = i % k
    return np.array([gf[g] for g in groups])


def oof_scores(X, y, w, fold, lam):
    s = np.zeros(len(y))
    for k in sorted(set(fold)):
        tr, te = fold != k, fold == k
        if y[tr].min() == y[tr].max():
            s[te] = -5.0 if y[tr][0] == 0 else 5.0
            continue
        s[te] = decision(fit_logit(X[tr], y[tr], w[tr], lam), X[te])
    return s


def logloss(p, y, w):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.average(y * np.log(p) + (1 - y) * np.log(1 - p), weights=w))


# ---------------------------------------------------------------- metrics
def auc(score, y):
    y = np.asarray(y)
    n1, n0 = int(y.sum()), int((1 - y).sum())
    if n1 == 0 or n0 == 0:
        return None
    order = np.argsort(score, kind="mergesort")
    r = np.empty(len(score))
    s = np.asarray(score)[order]
    i = 0
    ranks = np.empty(len(score))
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        ranks[i:j + 1] = (i + j) / 2 + 1
        i = j + 1
    r[order] = ranks
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def prf(pred, y, w=None):
    """Precision/recall/F1; counts (tp/fp/fn) are raw, the ratios use sampling weights `w` (population estimate) if given."""
    pred, y = np.asarray(pred, bool), np.asarray(y, bool)
    n_tp, n_fp, n_fn = int((pred & y).sum()), int((pred & ~y).sum()), int((~pred & y).sum())
    w = np.ones(len(y)) if w is None else np.asarray(w, float)
    tp, fp, fn = float(w[pred & y].sum()), float(w[pred & ~y].sum()), float(w[~pred & y].sum())
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p and r else (0.0 if tp + fp + fn else None)
    return {"precision": p, "recall": r, "f1": f1, "tp": n_tp, "fp": n_fp, "fn": n_fn, "n_pos": n_tp + n_fn, "n": len(y)}


def pick_threshold(p, y, min_precision=0.75, w=None):
    """Operating point: highest recall with precision >= min_precision; else best F1. -> (threshold, achieved_precision_target)"""
    best = None
    cand = np.unique(np.round(p, 3))
    rows = []
    for t in cand:
        m = prf(p >= t, y, w)
        if m["precision"] is None:
            continue
        rows.append((t, m))
    ok = [(t, m) for t, m in rows if m["precision"] >= min_precision and m["recall"]]
    if ok:
        t = max(ok, key=lambda x: (x[1]["recall"], x[1]["precision"]))[0]
        return float(t), True
    if rows:
        t = max(rows, key=lambda x: x[1]["f1"] or 0)[0]
        return float(t), False
    return 0.5, False


# ---------------------------------------------------------------- training
def _label_vec(recs, issue, k):
    ys = np.array([-1 if r["targets"].get(issue) is None else int(r["targets"][issue] >= k) for r in recs])
    return ys


def train_issue(F, recs, issue, seed=7, k=5, lams=(0.1, 1.0, 10.0, 100.0)):
    """Fit severity heads (>=1, >=2, >=3) for one issue. Returns (model dict, oof calibrated P(>=2), mask of rows used)."""
    names = FEATURES[issue]
    X = np.array([vec(f, names) for f in F])
    heads, oof2, used2 = {}, None, None
    for thr in (2, 1, 3):
        y = _label_vec(recs, issue, thr)
        use = y >= 0
        w = np.array([r["weight"] * r.get("sw", 1.0) for r in recs])
        pos = int(y[use].sum())
        if use.sum() < 20 or pos < (12 if thr == 3 else 6) or use.sum() - pos < 6:
            continue
        Xu, yu, wu = X[use], y[use].astype(float), w[use]
        grp = [recs[i].get("base", recs[i]["id"]) for i in np.flatnonzero(use)]
        fold = folds(grp, (yu * 1).astype(int).tolist(), k, seed)
        best = None
        for lam in lams:
            s = oof_scores(Xu, yu, wu, fold, lam)
            a, b = fit_platt(s, yu, wu)
            ll = logloss(_sigmoid(a * s + b), yu, wu)
            if best is None or ll < best[0]:
                best = (ll, lam, s, a, b)
        _, lam, s, a, b = best
        m = fit_logit(Xu, yu, wu, lam)
        heads[str(thr)] = {"mu": m[0].tolist(), "sd": m[1].tolist(), "coef": m[2].tolist(), "bias": m[3], "platt": [a, b],
                           "lam": lam, "n": int(use.sum()), "n_pos": pos}
        if thr == 2:
            oof2 = _sigmoid(a * s + b)
            used2 = use
            ids_oof = np.flatnonzero(use)
    if "2" not in heads:
        return None, None, None
    p = np.full(len(recs), np.nan)
    p[ids_oof] = oof2
    y2 = _label_vec(recs, issue, 2)
    t, met = pick_threshold(oof2, y2[used2], 0.75, np.array([r["weight"] * r.get("sw", 1.0) for r in recs])[used2])
    return {"features": names, "heads": heads, "threshold": t, "precision_target_met": met}, p, used2


def head_prob(h, x):
    return float(_sigmoid(h["platt"][0] * decision((np.array(h["mu"]), np.array(h["sd"]), np.array(h["coef"]), h["bias"]), x[None])[0] + h["platt"][1]))


def predict_issue(im, f):
    """-> (P(>=2), severity 0-3, {k: P(>=k)}) for one derived-feature dict."""
    x = vec(f, im["features"])
    ps = {int(k): head_prob(h, x) for k, h in im["heads"].items()}
    p2 = ps[2]
    p1 = max(ps.get(1, p2), p2)
    p3 = min(ps.get(3, p2 * 0.5), p2)
    sev = int(p1 >= 0.5) + int(p2 >= im["threshold"]) + int(p3 >= 0.5)
    return p2, min(sev, 3), {1: p1, 2: p2, 3: p3}


def fit_tilt(F, recs, lam=3.0):
    """Ridge regression of the signed tilt (deg) on the Hough / orientation estimates; rows with a trustworthy label only."""
    rows = [i for i, r in enumerate(recs) if r["tilt_deg"] is not None and r["source"] != "weak"]
    if len(rows) < 25:
        return None
    X = np.array([vec(F[i], TILT_REG) for i in rows])
    y = np.array([np.clip(recs[i]["tilt_deg"], -10, 10) for i in rows])
    w = np.array([recs[i]["tilt_conf"] for i in rows])
    mu = np.average(X, axis=0, weights=w)
    Z = np.hstack([X - mu, np.ones((len(X), 1))])
    R = np.eye(Z.shape[1]) * lam
    R[-1, -1] = 0
    beta = np.linalg.solve((Z * w[:, None]).T @ Z + R, (Z * w[:, None]).T @ y)
    return {"features": TILT_REG, "mu": mu.tolist(), "coef": beta[:-1].tolist(), "bias": float(beta[-1]), "n": len(rows)}


def predict_tilt(tm, f):
    if not tm:
        return float(f["hough_angle"])
    x = vec(f, tm["features"]) - np.array(tm["mu"])
    return float(np.clip(x @ np.array(tm["coef"]) + tm["bias"], -10, 10))


def add_tilt_hat(F, tm):
    for f in F:
        f["abs_tilt_hat"] = abs(predict_tilt(tm, f))


def save(model, path):
    Path(path).write_text(json.dumps(model, indent=1, sort_keys=True) + "\n", "utf-8")


def load(path=None):
    p = Path(path) if path else DEFAULT_MODEL
    return json.loads(p.read_text("utf-8")) if p.exists() else None
