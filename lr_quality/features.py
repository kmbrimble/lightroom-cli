"""v2 feature extraction: flat dict of floats feeding the learned model. numpy only (cv2 never required)."""
from __future__ import annotations

import numpy as np


def _label_cells(mask):
    """4-connected component labels of a small boolean grid (min-label propagation; capped iterations)."""
    h, w = mask.shape
    big = h * w + 1
    lab = np.where(mask, np.arange(1, h * w + 1).reshape(h, w), big).astype(np.int32)
    for _ in range(300):
        p = np.pad(lab, 1, constant_values=big)
        new = np.minimum(lab, np.minimum(np.minimum(p[:-2, 1:-1], p[2:, 1:-1]), np.minimum(p[1:-1, :-2], p[1:-1, 2:])))
        new = np.where(mask, new, big)
        if np.array_equal(new, lab):
            break
        lab = new
    return lab, big


def _box(a, k):
    """Mean over a kxk window (valid-size output, cumulative sums)."""
    c = np.pad(a.astype(np.float32), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def _blocks(a, b):
    h, w = a.shape[0] // b * b, a.shape[1] // b * b
    return a[:h, :w].reshape(h // b, b, w // b, b).mean((1, 3))


def _highlights(rgb, q, hi):
    q, rgb = q[::2, ::2], rgb[::2, ::2]  # half-res is plenty for area statistics
    H, W = q.shape
    m = q >= hi
    mx = rgb.max(2) >= hi + 2
    f = {"hi_pct": float(m.mean() * 100), "hi_pct_240": float((q >= 240).mean() * 100), "hi_anych_pct": float(mx.mean() * 100)}
    cell = _blocks(m.astype(np.float32), 4) >= 0.5
    lab, big = _label_cells(cell)
    ncell = cell.size
    areas = np.bincount(lab[cell].ravel()) if cell.any() else np.zeros(1, int)
    sizes = np.zeros_like(lab, dtype=np.float32)
    if cell.any():
        sizes[cell] = areas[lab[cell]] / ncell * 100  # blob area, % of frame
    big_cells = cell & (sizes >= 0.25)
    cellpix = _blocks(m.astype(np.float32), 4)
    f["hi_big_pct"] = float(cellpix[big_cells].sum() / ncell * 100)
    f["hi_small_pct"] = f["hi_pct"] - f["hi_big_pct"] if f["hi_pct"] > f["hi_big_pct"] else 0.0
    f["hi_max_blob_pct"] = float(sizes.max()) if cell.any() else 0.0
    f["hi_blobs"] = float(len(np.unique(lab[big_cells]))) if big_cells.any() else 0.0
    # detail loss: clipped pixels whose whole 7x7 neighbourhood is clipped
    f["hi_interior_pct"] = float((cellpix >= 0.999).mean() * 100)
    # where: share of clipped pixels in the top third (sky) vs the lower two thirds (foreground)
    h3 = H // 3
    f["hi_top_pct"] = float(m[:h3].mean() * 100)
    f["hi_lower_pct"] = float(m[h3:].mean() * 100)
    return f


def _shadows(q, lo):
    m = q <= lo
    f = {"lo_pct": float(m.mean() * 100), "lo_pct_16": float((q <= 16).mean() * 100), "lo_pct_30": float((q <= 30).mean() * 100)}
    f["lo_interior_pct"] = float((_blocks((q <= 12).astype(np.float32), 8) >= 0.999).mean() * 100)
    return f


def _exposure(q):
    H, W = q.shape
    hist = np.bincount(q.ravel(), minlength=256)
    cdf = np.cumsum(hist)
    lv = np.arange(256)
    pc = [int(np.searchsorted(cdf, p / 100.0 * q.size, side="left")) for p in (1, 5, 25, 50, 75, 95, 99)]
    n = q.size
    mean = float((hist * lv).sum() / n)
    c = q[H // 4: 3 * H // 4: 2, W // 4: 3 * W // 4: 2]
    return {"mean": mean, "log_mean": float((hist * np.log1p(lv)).sum() / n), "center_mean": float(c.mean()),
            "lower_mean": float(q[H // 3::2, ::2].mean()), "p01": pc[0], "p05": pc[1], "p25": pc[2], "p50": pc[3],
            "p75": pc[4], "p95": pc[5], "p99": pc[6], "range": pc[6] - pc[0],
            "std": float(np.sqrt(max((hist * lv * lv).sum() / n - mean * mean, 0)))}


def _sobel(g):
    gx = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[1:-1, :-2] + g[2:, :-2])
    gy = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[:-2, 1:-1] + g[:-2, 2:])
    return gx, gy


def _tilesum(a, ty, tx):
    th, tw = a.shape[0] // ty, a.shape[1] // tx
    return a[: th * ty, : tw * tx].reshape(ty, th, tx, tw).mean((1, 3)).ravel()


def _focus(g, lap, gx, gy, q):
    ty, tx = 12, 16
    ty = max(1, min(ty, lap.shape[0] // 8))
    tx = max(1, min(tx, lap.shape[1] // 8))
    lap2 = lap * lap
    tv = np.maximum(_tilesum(lap2, ty, tx) - _tilesum(lap, ty, tx) ** 2, 0)  # per-tile Laplacian variance
    mag = np.sqrt(gx * gx + gy * gy)
    e1 = _tilesum(mag, ty, tx)
    # coarse scale: 2x2 box downsample
    g2 = _blocks(g, 2)
    gx2, gy2 = _sobel(g2)
    mag2 = np.sqrt(gx2 * gx2 + gy2 * gy2)
    e2 = _tilesum(mag2, ty, tx)
    ex1, ey1 = _tilesum(np.abs(gx), ty, tx), _tilesum(np.abs(gy), ty, tx)
    ex2, ey2 = _tilesum(np.abs(gx2), ty, tx), _tilesum(np.abs(gy2), ty, tx)
    tl = _tilesum(g[1:-1, 1:-1], ty, tx)
    valid = (tl > 8) & (tl < 245)
    if valid.sum() < 3:
        valid = np.ones_like(valid)
    idx = np.flatnonzero(valid)
    k = max(3, int(round(0.15 * idx.size)))
    top = idx[np.argsort(-e1[idx])[:k]]  # strongest-edge tiles: the in-focus subject candidates
    eps = 1e-3
    ratio = np.log((e1[top] + eps) / (e2[top] * 2 + eps))  # fine/coarse gradient energy (2x2 box halves a step edge's slope)
    rx = np.log((ex1[top] + eps) / (ex2[top] * 2 + eps))
    ry = np.log((ey1[top] + eps) / (ey2[top] * 2 + eps))
    ms = mag[::2, ::2]
    m1 = ms
    strong = ms >= np.percentile(ms, 98)
    acut = float(np.abs(lap[::2, ::2][strong]).mean() / (ms[strong].mean() + eps))
    lv = tv[idx]
    best = float(lv.max())
    return {
        "foc_lv_max": best, "foc_lv_p90": float(np.percentile(lv, 90)), "foc_lv_p75": float(np.percentile(lv, 75)),
        "foc_lv_med": float(np.median(lv)), "foc_top_ratio": float(np.median(ratio)), "foc_top_ratio_x": float(np.median(rx)),
        "foc_top_ratio_y": float(np.median(ry)), "foc_ratio_min": float(min(np.median(rx), np.median(ry))),
        "foc_ratio_aniso": float(abs(np.median(rx) - np.median(ry))), "foc_acutance": acut,
        "foc_edge_p99": float(np.percentile(m1, 99)), "foc_top_e1": float(e1[top].mean()),
        "foc_tex_frac": float((tv > 20).mean()), "foc_flat_frac": float((tv < 3).mean()),
        # relative to the image's own best tile: how many tiles are nearly as sharp as the sharpest
        "foc_near_best_frac": float((tv >= 0.25 * max(best, 1e-6)).mean()),
        "foc_global": float(lap.var()),
    }, tv, valid


def _noise(imm, tv, valid, g):
    ty, tx = 12, 16
    ty = max(1, min(ty, imm.shape[0] // 8))
    tx = max(1, min(tx, imm.shape[1] // 8))
    ia = _tilesum(np.abs(imm), ty, tx)
    idx = np.flatnonzero(valid)
    take = idx[np.argsort(tv[idx], kind="stable")[: max(1, idx.size // 4)]]
    return {"noise_sigma": float(np.sqrt(np.pi / 2) / 6.0 * ia[take].mean())}


def _ori(gx, gy, y0, y1, vertical):
    """Magnitude-weighted orientation histogram of near-horizontal (or near-vertical) edges.
    Angle sign matches Hough: >0 = horizon descends to the right. -> (peak_deg, peak_share, mass_frac, level_share)."""
    a, b = gx[y0:y1:2, ::2], gy[y0:y1:2, ::2]
    mag = np.sqrt(a * a + b * b)
    sel = mag >= 80
    tot = float(mag[sel].sum())
    if tot <= 0:
        return 0.0, 0.0, 0.0, 0.0
    a, b, mag = a[sel], b[sel], mag[sel]
    if vertical:
        s = np.where(a < 0, -1, 1)
        th = np.degrees(np.arctan2(b * s, a * s))
    else:
        s = np.where(b < 0, -1, 1)
        th = np.degrees(np.arctan2(-a * s, b * s))
    near = np.abs(th) <= 10
    if not near.any():
        return 0.0, 0.0, 0.0, 0.0
    th, mag = th[near], mag[near]
    nm = float(mag.sum())
    bins = np.arange(-10, 10.001, 0.25)
    h, _ = np.histogram(th, bins=bins, weights=mag)
    sm = np.convolve(h, np.ones(5) / 5, mode="same")
    c = (bins[:-1] + bins[1:]) / 2
    pk = float(c[int(sm.argmax())])
    w = np.abs(th - pk) <= 1.0
    peak = float((th[w] * mag[w]).sum() / mag[w].sum())
    return peak, float(mag[w].sum() / nm), nm / tot, float(mag[np.abs(th) <= 0.3].sum() / nm)


def _sky(rgb, tv_flat_frac):
    H = rgb.shape[0]
    top = rgb[: H // 3: 4, ::4].reshape(-1, 3).astype(np.float32).mean(0)
    return {"blue_top": float((top[2] - top[0]) / 255.0), "top_mean": float(top.mean())}


def extract(rgb, q, g, lap, imm, hough, th):
    """-> flat dict of floats. hough = core._tilt() output or None."""
    c = th["clip"]
    f = {}
    f.update(_highlights(rgb, q, c["high_level"]))
    f.update(_shadows(q, c["low_level"]))
    f.update({"exp_" + k: v for k, v in _exposure(q).items()})
    gx, gy = _sobel(g)
    fo, tv, valid = _focus(g, lap, gx, gy, q)
    f.update(fo)
    f.update(_noise(imm, tv, valid, g))
    H = gx.shape[0]
    pk, sh, mf, lv = _ori(gx, gy, int(0.10 * H), int(0.75 * H), False)
    f.update({"ori_h_peak": pk, "ori_h_share": sh, "ori_h_massfrac": mf, "ori_h_level": lv})
    pk, sh, mf, lv = _ori(gx, gy, 0, H, True)
    f.update({"ori_v_peak": pk, "ori_v_share": sh, "ori_v_massfrac": mf, "ori_v_level": lv})
    f["hough_angle"] = hough["angle_deg"] if hough else 0.0
    f["hough_conf"] = hough["confidence"] if hough else 0.0
    f["hough_lines"] = float(hough["lines_used"]) if hough else 0.0
    f["hough_valid"] = 1.0 if hough else 0.0
    f.update(_sky(rgb, f["foc_flat_frac"]))
    return {k: float(v) for k, v in f.items()}
