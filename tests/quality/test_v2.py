"""lr_quality v2: features, label adapters, learning loop. Synthetic images only (no private photos or labels)."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lr_quality  # noqa: E402
from lr_quality import cli, labels, learn, model  # noqa: E402

from . import synth  # noqa: E402
from PIL import Image  # noqa: E402


def feats(im):
    return lr_quality.analyze(im, model=None)["features"]


def test_big_blown_area_vs_specular_dots():
    a = np.asarray(synth.textured(seed=5, lo=60, hi=180)).copy()
    big = a.copy()
    big[100:400, 200:700] = 255
    dots = a.copy()
    for y in range(40, 760, 60):
        for x in range(40, 1160, 60):
            dots[y:y + 2, x:x + 2] = 255
    fb, fd = feats(Image.fromarray(big)), feats(Image.fromarray(dots))
    assert fb["hi_big_pct"] > 10 and fb["hi_interior_pct"] > 10
    assert fd["hi_big_pct"] < 1 and fd["hi_interior_pct"] < 1 and fd["hi_small_pct"] > 0


def test_focus_ratio_drops_when_blurred_and_flat_sky_is_ignored():
    sharp = feats(synth.sky_over_texture(0.0))
    soft = feats(synth.sky_over_texture(2.5))
    assert soft["foc_top_ratio"] < sharp["foc_top_ratio"] - 0.3
    assert sharp["foc_flat_frac"] > 0.4  # the featureless sky is measured as flat, not as "soft"


@pytest.mark.parametrize("angle", [2.0, -2.5])
def test_tilt_feature_follows_horizon(angle):
    f = feats(synth.horizon(angle))
    assert f["st_h_share"] > 0.5 and abs(f["st_h_peak"] - angle) < 0.7


def test_rubric_adapter_and_tilt_rules():
    r = labels.from_rubric({"id": 7, "highlights": 2, "shadows": 0, "exposure": -2, "focus": 1, "motion_blur": 3, "noise": 0,
                            "horizon_tilt_deg": 1.5, "tilt_confidence": 0.8, "kind": "hdr"})
    t = r["targets"]
    assert t["blown_highlights"] == 2 and t["underexposed"] == 2 and t["overexposed"] == 0 and t["blur"] == 3
    assert t["tilted_horizon"] == 2 and r["tilt_deg"] == 1.5
    low = labels.from_rubric({"id": 8, "highlights": 0, "shadows": 0, "exposure": 0, "focus": 0, "motion_blur": 0, "noise": 0,
                              "horizon_tilt_deg": 5, "tilt_confidence": 0.3})
    assert low["targets"]["tilted_horizon"] == 0 and low["tilt_deg"] is None  # untrusted reference is not a positive


def test_weak_and_verdict_adapters(tmp_path):
    weak = {"photo_id": "1", "fields": {"visible_problems": ["none"]},
            "claude": {"fields": {"visible_problems": ["blown_highlights", "blurry"], "scene_type": "x"}}}
    assert labels.from_weak({"photo_id": "2", "fields": {"visible_problems": ["blurry"]}}) is None  # local-only: ignored
    w = labels.from_weak(weak)
    assert w["weight"] < 1 and w["targets"]["blown_highlights"] == 2 and w["targets"]["blur"] == 2 and w["targets"]["noisy"] is None
    v = labels.from_verdict({"id": 3, "verdict": "reject", "issues": ["soft_focus"]})
    assert v["targets"]["soft_focus"] == 2 and v["targets"]["blown_highlights"] is None
    p = tmp_path / "r.ndjson"
    p.write_text(json.dumps({"id": "1", "highlights": 0, "shadows": 0, "exposure": 0, "focus": 0, "motion_blur": 0, "noise": 0}) + "\n")
    q = tmp_path / "w.ndjson"
    q.write_text(json.dumps(weak) + "\n")
    got = labels.load([p], weak=[q])
    assert len(got) == 1 and got[0]["source"] == "rubric"  # a real review beats a weak one


def test_heldout_split_is_stable_and_about_a_quarter():
    ids = [str(i) for i in range(4000)]
    ho = [labels.is_heldout(i) for i in ids]
    assert 0.22 < np.mean(ho) < 0.28 and ho == [labels.is_heldout(i) for i in ids]


def test_logit_auc_and_grouped_folds():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, 3))
    y = (X[:, 0] + 0.3 * rng.normal(size=300) > 0).astype(float)
    m = model.fit_logit(X, y, np.ones(300), 1.0)
    assert model.auc(model.decision(m, X), y) > 0.95 and m[2][0] > abs(m[2][1]) * 3
    groups = [i // 6 for i in range(60)]
    f = model.folds(groups, [g % 2 for g in groups], 5, 1)
    assert all(len(set(f[i * 6:(i + 1) * 6])) == 1 for i in range(10))  # variants of one photo never straddle folds


def _dataset(tmp_path, n=90):
    """Textured images with a bright block of varying size; review: highlights 2 when the block exceeds 8% of the frame."""
    cache, revs = [], []
    for i in range(n):
        frac = (i % 10) / 10 * 0.3
        a = np.asarray(synth.textured(600, 400, seed=i, lo=40, hi=180)).copy()
        a[: int(400 * frac), :] = 255
        r = lr_quality.analyze(Image.fromarray(a), model=None)
        cache.append({"id": f"s{i}", "result": r})
        revs.append({"id": f"s{i}", "highlights": 2 if frac > 0.08 else 0, "shadows": 0, "exposure": 0, "focus": 0, "motion_blur": 0,
                     "noise": 0})
    fp, rp = tmp_path / "f.ndjson", tmp_path / "r.ndjson"
    fp.write_text("".join(json.dumps(c) + "\n" for c in cache))
    rp.write_text("".join(json.dumps(c) + "\n" for c in revs))
    return fp, rp


def test_learn_cli_end_to_end_and_refuses_worse(tmp_path, capsys):
    fp, rp = _dataset(tmp_path)
    out = tmp_path / "m.json"
    assert cli.main(["learn", "--reviews", str(rp), "--features", str(fp), "--out", str(out)]) == 0
    m = json.loads(out.read_text())
    assert m["schema"] == model.MODEL_SCHEMA and "blown_highlights" in m["issues"] and m["dev_model"]["issues"]
    r = lr_quality.analyze(synth.flat(255), model=str(out))
    assert r["assessment"]["issues"]["blown_highlights"]["noticeable"] and {f["code"] for f in r["flags"]} >= {"blown_highlights"}
    assert r["flags_v1"] and r["assessment"]["fixes"]
    ok = lr_quality.analyze(synth.textured(), model=str(out))
    assert not ok["assessment"]["issues"]["blown_highlights"]["noticeable"]
    # incremental use: if new reviews corrupt the training side only (inverted labels on dev rows), the held-out split still tells
    # the truth, so the retrained model is worse there and must be refused
    rows = [json.loads(x) for x in rp.read_text().splitlines()]
    bad = tmp_path / "bad.ndjson"
    bad.write_text("".join(json.dumps({**x, "id": x["id"], "highlights": x["highlights"] if labels.is_heldout(x["id"]) else 2 - x["highlights"]}) + "\n" for x in rows))
    before = out.read_text()
    code = cli.main(["learn", "--reviews", str(bad), "--features", str(fp), "--out", str(out)])
    assert code == 1 and out.read_text() == before
    assert cli.main(["learn", "--reviews", str(bad), "--features", str(fp), "--out", str(out), "--force"]) == 0


def test_second_opinion_budget_respected(tmp_path):
    fp, rp = _dataset(tmp_path)
    recs = labels.load([rp])
    kept, F, _ = learn.join(recs, learn.load_cache(fp))
    m, oof, used = learn.fit_all(F, kept)
    real = np.ones(len(kept), bool)
    assert learn._ambiguous(m, oof, m["second_opinion"]["width"])[real].mean() <= learn.MAX_SECOND_OPINION + 1e-9


def test_shipped_model_loads_and_assesses():
    r = lr_quality.analyze(synth.textured())
    if "assessment" in r:  # shipped model present
        assert set(r["assessment"]["issues"]) >= {"blown_highlights", "crushed_shadows", "tilted_horizon"}
        json.dumps(r)
