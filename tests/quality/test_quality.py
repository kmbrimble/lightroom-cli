import json
import subprocess
import sys

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
from PIL import Image, ImageFilter  # noqa: E402

import lr_quality  # noqa: E402
from lr_quality import core  # noqa: E402

from . import synth  # noqa: E402

CODES = lambda r: {f["code"] for f in r["flags"]}  # noqa: E731


def test_schema_keys_present():
    r = lr_quality.analyze(synth.textured())
    assert r["schema"] == "lr_quality/1"
    assert set(r) == {"schema", "source", "scene_hint", "image", "exposure", "focus", "tilt", "noise", "flags", "needs_second_opinion"}
    assert set(r["image"]) == {"width", "height", "analysed_long_edge", "format"}
    keys = {"mean_luma", "p01", "p05", "p25", "p50", "p75", "p95", "p99", "clip_high_pct", "clip_low_pct",
            "clip_high_pct_by_channel", "upper_third_clip_high_pct", "midtone_mass", "histogram_luma",
            "dynamic_range_p99_p01", "vs_targets", "batch_percentile"}
    assert set(r["exposure"]) == keys
    assert set(r["exposure"]["clip_high_pct_by_channel"]) == {"r", "g", "b"}
    assert len(r["exposure"]["histogram_luma"]) == 32 and abs(sum(r["exposure"]["histogram_luma"]) - 1) < 1e-3
    assert set(r["focus"]) == {"score_tile_p90", "score_global", "sharp_tile_fraction", "method", "relative_to_batch"}
    assert set(r["noise"]) == {"sigma_estimate"}
    assert set(r["needs_second_opinion"]) == {"value", "reasons"}
    json.dumps(r)


def test_deterministic_and_resized():
    im = synth.textured(2400, 1600)
    a, b = lr_quality.analyze(im), lr_quality.analyze(im)
    assert a == b
    assert a["image"]["width"] == 2400 and a["image"]["analysed_long_edge"] == 1600


def test_blur_monotonic():
    im = synth.textured()
    scores = [lr_quality.analyze(im.filter(ImageFilter.GaussianBlur(r)) if r else im)["focus"] for r in (0, 0.7, 1.2, 2, 3, 5)]
    for k in ("score_tile_p90", "score_global"):
        v = [s[k] for s in scores]
        assert all(x > y for x, y in zip(v, v[1:])), v


def test_smooth_sky_not_flagged_as_soft():
    full, sky = lr_quality.analyze(synth.textured()), lr_quality.analyze(synth.sky_over_texture())
    assert sky["focus"]["score_tile_p90"] > 0.8 * full["focus"]["score_tile_p90"]
    assert sky["focus"]["score_global"] < 0.7 * full["focus"]["score_global"]  # a global metric would have penalised it
    assert "soft_focus" not in CODES(sky)
    assert "soft_focus" in CODES(lr_quality.analyze(synth.sky_over_texture(blur=5)))


def test_clipping_exact():
    a = np.full((400, 600, 3), 128, np.uint8)
    a[:40] = 255  # 10% rows white
    a[-20:] = 0  # 5% black
    e = lr_quality.analyze(Image.fromarray(a))["exposure"]
    assert e["clip_high_pct"] == 10.0 and e["clip_low_pct"] == 5.0
    assert e["clip_high_pct_by_channel"] == {"r": 10.0, "g": 10.0, "b": 10.0}
    assert e["upper_third_clip_high_pct"] == pytest.approx(40 / 133 * 100, abs=1e-3)  # 40 of the top 133 rows
    b = a.copy()
    b[:, :, 1:] = 0
    b[:40, :, 1:] = 0  # only red saturated in the top band
    e = lr_quality.analyze(Image.fromarray(b))["exposure"]
    assert e["clip_high_pct_by_channel"]["r"] == 10.0 and e["clip_high_pct_by_channel"]["g"] == 0.0


def test_exposure_flags():
    assert "underexposed" in CODES(lr_quality.analyze(synth.flat(25)))
    assert "overexposed" in CODES(lr_quality.analyze(synth.flat(225)))
    ok = lr_quality.analyze(synth.textured())
    assert not CODES(ok) & {"underexposed", "overexposed", "blown_highlights", "crushed_shadows"}
    a = np.full((400, 600, 3), 128, np.uint8)
    a[:60] = 255
    r = lr_quality.analyze(Image.fromarray(a))
    assert "blown_highlights" in CODES(r)  # 15%
    a = np.full((400, 600, 3), 128, np.uint8)
    a[:100] = 0
    assert "crushed_shadows" in CODES(lr_quality.analyze(Image.fromarray(a)))


@pytest.mark.parametrize("angle", [-3.5, -2.0, -1.0, 0.6, 1.5, 3.0])
def test_tilt_recovered(angle):
    t = lr_quality.analyze(synth.horizon(angle))["tilt"]
    assert t is not None and abs(t["angle_deg"] - angle) < 0.5 and t["lines_used"] >= 2


def test_tilt_flag_and_none():
    assert "tilted_horizon" in CODES(lr_quality.analyze(synth.horizon(2.0)))
    assert "tilted_horizon" not in CODES(lr_quality.analyze(synth.horizon(0.1)))
    assert "tilted_horizon" not in CODES(lr_quality.analyze(synth.horizon(8.0)))
    assert lr_quality.analyze(synth.noise_image())["tilt"] is None
    assert lr_quality.analyze(synth.flat(128))["tilt"] is None


def test_noise_estimate():
    base = np.asarray(synth.flat(128, 1000, 700)).astype(float)
    n = np.random.default_rng(0).normal(0, 8, base.shape[:2])[..., None]  # same noise in all channels -> luma sigma 8
    s = lr_quality.analyze(Image.fromarray(np.clip(base + n, 0, 255).astype(np.uint8)))
    assert 6.5 < s["noise"]["sigma_estimate"] < 9.5 and "noisy" in CODES(s)
    assert "noisy" not in CODES(lr_quality.analyze(synth.textured()))


def test_second_opinion_near_threshold():
    a = np.full((400, 600, 3), 128, np.uint8)
    a[:7] = 255  # 1.75% vs warn 1.5
    r = lr_quality.analyze(Image.fromarray(a))
    assert r["needs_second_opinion"]["value"] and any("blown_highlights" in x for x in r["needs_second_opinion"]["reasons"])
    a[:] = 128
    a[:100] = 255  # 25%: far above, no near-miss reason for it
    r = lr_quality.analyze(Image.fromarray(a))
    assert not any(x.startswith("blown_highlights") for x in r["needs_second_opinion"]["reasons"])


def test_second_opinion_scene_conflict_and_unusual():
    a = np.full((400, 600, 3), 128, np.uint8)
    a[:60] = 255
    plain = lr_quality.analyze(Image.fromarray(a))
    sun = lr_quality.analyze(Image.fromarray(a), scene_hint="sunset")
    assert not any("scene_hint" in x for x in plain["needs_second_opinion"]["reasons"])
    assert any("sunset" in x for x in sun["needs_second_opinion"]["reasons"])
    assert any("very small" in x for x in lr_quality.analyze(synth.textured(120, 100))["needs_second_opinion"]["reasons"])
    assert any("uniform" in x for x in lr_quality.analyze(synth.flat(100))["needs_second_opinion"]["reasons"])
    small_sharp = np.full((800, 1200, 3), 120, np.uint8)
    small_sharp[:300, :450] = np.asarray(synth.textured(450, 300))  # ~9 of 64 tiles
    r = lr_quality.analyze(Image.fromarray(small_sharp))
    assert any("conflict" in x for x in r["needs_second_opinion"]["reasons"])


def test_thresholds_override():
    r = lr_quality.analyze(synth.flat(100), thresholds={"luma": {"under_warn": 0.5}})
    assert "underexposed" in CODES(r)
    assert "underexposed" not in CODES(lr_quality.analyze(synth.flat(100)))


def test_vs_targets():
    t = {"scenes": {"default": {"mean_luma": 100.0, "p50": 100}, "city": {"mean_luma": 90.0, "p50": 90}}}
    e = lr_quality.analyze(synth.flat(128), targets=t)["exposure"]["vs_targets"]
    assert e == {"scene": "default", "delta": {"mean_luma": 28.0, "p50": 28}}
    assert lr_quality.analyze(synth.flat(128), targets=t, scene_hint="city")["exposure"]["vs_targets"]["scene"] == "city"
    assert lr_quality.analyze(synth.flat(128))["exposure"]["vs_targets"] is None


def test_cv2_parity():
    cv2 = pytest.importorskip("cv2")  # noqa: F841
    im = synth.sky_over_texture()
    a, b = lr_quality.analyze(im), lr_quality.analyze(im, accel="numpy")
    for k in ("score_tile_p90", "score_global"):
        assert a["focus"][k] == pytest.approx(b["focus"][k], rel=1e-3)
    assert a["noise"]["sigma_estimate"] == pytest.approx(b["noise"]["sigma_estimate"], rel=1e-3)
    assert a["flags"] == b["flags"] and a["exposure"] == b["exposure"]


# ---- batch + CLI ----
def run(*args, stdin=None):
    return subprocess.run([sys.executable, "-m", "lr_quality", *args], input=stdin, capture_output=True)


def test_cli_analyze_stdin_roundtrip():
    p = run("analyze", "-", "--scene-hint", "landscape", stdin=synth.jpeg_bytes(synth.textured()))
    assert p.returncode == 0, p.stderr
    assert p.stdout.count(b"\n") == 1
    r = json.loads(p.stdout)
    assert r["schema"] == "lr_quality/1" and r["image"]["format"] == "JPEG" and r["scene_hint"] == "landscape"
    assert r["focus"]["relative_to_batch"] is None and r["source"] is None


def test_cli_analyze_bad_input_and_usage():
    p = run("analyze", "-", stdin=b"not an image")
    assert p.returncode == 1 and "error" in json.loads(p.stdout)
    assert run("analyze", "x.jpg", "--scene-hint", "bogus").returncode == 2
    assert run().returncode == 2
    assert run("batch").returncode == 2


@pytest.fixture
def folder(tmp_path):
    base = synth.textured()
    for i, r in enumerate([0, 0, 0.3, 0.5, 0, 6]):
        (base.filter(ImageFilter.GaussianBlur(r)) if r else base).save(tmp_path / f"i{i}.jpg", quality=95)
    (tmp_path / "bad.jpg").write_bytes(b"garbage")
    return tmp_path


def test_batch_relative_errors_and_out(folder, tmp_path):
    out = tmp_path / "o.ndjson"
    paths = sorted(str(p) for p in folder.glob("*.jpg"))
    p = run("batch", *paths, "--out", str(out))
    assert p.returncode == 1 and p.stdout == b""
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(rows) == 7
    bad = [r for r in rows if "error" in r]
    assert len(bad) == 1 and bad[0]["source"].endswith("bad.jpg")
    good = {r["source"].rsplit("/", 1)[-1]: r for r in rows if "error" not in r}
    assert good["i5.jpg"]["focus"]["relative_to_batch"] < 0.28
    assert "soft_focus" in CODES(good["i5.jpg"])
    assert good["i0.jpg"]["focus"]["relative_to_batch"] > 0.9 and "soft_focus" not in CODES(good["i0.jpg"])
    pct = [r["exposure"]["batch_percentile"] for r in good.values()]
    assert all(0 <= x <= 100 for x in pct)


def test_batch_list_and_stdin_list_and_all_ok(folder):
    (folder / "bad.jpg").unlink()
    lst = folder / "l.txt"
    lst.write_text("\n".join(str(p) for p in sorted(folder.glob("*.jpg"))))
    a = run("batch", "--list", str(lst))
    b = run("batch", "--stdin-list", stdin=lst.read_bytes())
    assert a.returncode == 0 and b.returncode == 0 and a.stdout == b.stdout
    assert len(a.stdout.splitlines()) == 6
    c = run("batch", str(folder))
    assert c.stdout == a.stdout


def test_batch_small_batch_has_no_relative(tmp_path):
    synth.textured().save(tmp_path / "a.jpg")
    r = json.loads(run("batch", str(tmp_path / "a.jpg")).stdout)
    assert r["focus"]["relative_to_batch"] is None and r["exposure"]["batch_percentile"] == 50.0


def test_learn_targets(tmp_path):
    (tmp_path / "city").mkdir()
    synth.flat(100).save(tmp_path / "city" / "a.png")
    synth.flat(120).save(tmp_path / "city" / "b.png")
    synth.flat(60).save(tmp_path / "top.png")
    out = tmp_path / "t.json"
    p = run("learn-targets", str(tmp_path), "--out", str(out))
    assert p.returncode == 0, p.stderr
    doc = json.loads(out.read_text())
    assert doc["scenes"]["city"]["n"] == 2 and doc["scenes"]["city"]["p50"] == 110
    assert doc["scenes"]["default"]["p50"] == 60
    t = tmp_path / "x.png"
    synth.flat(130).save(t)
    r = json.loads(run("analyze", str(t), "--targets", str(out), "--scene-hint", "city").stdout)
    assert r["exposure"]["vs_targets"]["delta"]["p50"] == 20


def test_import_isolation():
    code = "import sys, lr_quality; assert not {'lightroom_sdk','pydantic'} & set(sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
