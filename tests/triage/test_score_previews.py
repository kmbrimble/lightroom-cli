import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
from tests.quality import synth  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))  # importable by name so pool workers can unpickle _work
import score_previews as sp  # noqa: E402


def test_hint_for():
    assert sp.hint_for("IMG_1.ARW", "12") == "landscape"
    assert sp.hint_for("night_market.dng") == "night"


def test_score_resume_and_batch_fields(tmp_path):
    prev = tmp_path / "p"
    prev.mkdir()
    for i in range(6):
        synth.textured(800, 600).save(prev / f"{i + 1}.jpg")
    (prev / "7.jpg").write_bytes(b"not a jpeg")
    out = tmp_path / "q.ndjson"
    s = sp.run(prev, out, workers=2, log=lambda *_: None)
    assert (s["scored"], s["failed"]) == (6, 1)
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["id"] for r in rows] == ["1", "2", "3", "4", "5", "6"]
    assert rows[0]["schema"] == "lr_quality/2" and rows[0]["scene_hint"] == "landscape"
    assert rows[0]["focus"]["relative_to_batch"] is not None  # batch >= min_batch
    assert rows[0]["exposure"]["batch_percentile"] is not None
    # resume: nothing re-analysed (raw cache keeps the 6 successes)
    n = len((tmp_path / "q.ndjson.raw").read_text().splitlines())
    sp.run(prev, out, workers=2, log=lambda *_: None)
    assert len((tmp_path / "q.ndjson.raw").read_text().splitlines()) == n + 1  # only the failed one retried
