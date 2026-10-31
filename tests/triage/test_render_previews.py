import base64
import importlib.util
import json
from pathlib import Path

import pytest

from lightroom_sdk.client import LightroomClient

spec = importlib.util.spec_from_file_location("render_previews", Path(__file__).parents[2] / "scripts" / "render_previews.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

JPEG = base64.b64encode(b"\xff\xd8fakejpeg").decode()
PHOTOS = [{"id": i, "filename": f"p{i}.dng"} for i in range(1, 6)]


class FakeClient:
    """fail: {photo_id: n_failures_before_success}; shared across reconnects via class attrs."""
    fail, calls, connects = {}, [], 0

    async def connect(self):
        FakeClient.connects += 1

    async def disconnect(self):
        pass

    async def execute_command(self, cmd, params=None, timeout=30):
        if cmd == "catalog.getAllPhotos":
            o = params["offset"]
            return {"photos": PHOTOS[o:o + 2], "total": len(PHOTOS)}
        pid = params["photoId"]
        FakeClient.calls.append(pid)
        if FakeClient.fail.get(pid, 0) > 0:
            FakeClient.fail[pid] -= 1
            raise ConnectionError("boom")
        return {"preview": JPEG} if pid != 4 else {}


@pytest.fixture(autouse=True)
def reset():
    FakeClient.fail, FakeClient.calls, FakeClient.connects = {}, [], 0


async def test_render_retry_fail_and_resume(tmp_path):
    FakeClient.fail = {2: 1, 3: 99}  # 2 recovers on retry, 3 never does; 4 returns no preview
    s = await rp.run(FakeClient, tmp_path, log=lambda *_: None)
    assert (s["total"], s["rendered"], s["failed"]) == (5, 3, 2)
    assert sorted(p.stem for p in tmp_path.glob("*.jpg")) == ["1", "2", "5"]
    assert not list(tmp_path.glob("*.tmp"))
    recs = [json.loads(x) for x in (tmp_path / "render.ndjson").read_text().splitlines()]
    assert {r["id"]: r["ok"] for r in recs} == {1: True, 2: True, 3: False, 4: False, 5: True}
    assert "ConnectionError" in next(r for r in recs if r["id"] == 3)["error"]
    assert FakeClient.calls.count(3) == 3 and FakeClient.connects > 1  # 1 try + 2 retries, reconnected
    # resume: only the failures are retried
    FakeClient.calls.clear()
    s = await rp.run(FakeClient, tmp_path, log=lambda *_: None)
    assert s["skipped"] == 3 and set(FakeClient.calls) == {3, 4}


async def test_limit_and_ids(tmp_path):
    s = await rp.run(FakeClient, tmp_path / "a", limit=2, log=lambda *_: None)
    assert s["total"] == 2
    s = await rp.run(FakeClient, tmp_path / "b", ids={"5"}, log=lambda *_: None)
    assert s["rendered"] == 1 and (tmp_path / "b" / "5.jpg").exists()


async def test_against_mock_bridge(mock_lr_server, tmp_path):
    mock_lr_server.register_response("catalog.getAllPhotos", {"photos": PHOTOS[:3], "total": 3})
    mock_lr_server.register_response("preview.generatePreview", {"preview": JPEG})
    s = await rp.run(lambda: LightroomClient(port_file=str(mock_lr_server.port_file)), tmp_path, log=lambda *_: None)
    assert s["rendered"] == 3 and (tmp_path / "2.jpg").read_bytes() == b"\xff\xd8fakejpeg"
