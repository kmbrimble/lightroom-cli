"""lr-mcp-proxy over real stdio against the mock plugin + in-process daemon. No Lightroom involved."""
import base64
import json
import os
import sys

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from tests.daemon.conftest import daemon, make_daemon, mock  # noqa: F401  (fixtures)
from tests.daemon.mock_plugin import MockPlugin

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PNG = base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode()


@pytest_asyncio.fixture
async def client(daemon, mock, tmp_path):  # noqa: F811
    mock._orig_result = mock._result
    mock._result = lambda cmd, p: ({"preview": PNG, "info": {"size": p.get("size")}}
                                   if cmd == "preview.generatePreview" else mock._orig_result(cmd, p))
    t = StdioTransport(sys.executable, ["-u", "-m", "lightroom_sdk.mcp_proxy"], cwd=ROOT,
                       env={**os.environ, "LRD_HOME": daemon.home, "LR_PROXY_PREVIEW_MIN_INTERVAL": "0.5"})
    async with Client(t) as c:
        yield c


def last(mock):
    return mock.received[-1]


async def test_tool_list(client):
    names = {t.name for t in await client.list_tools()}
    assert names == {"lr_status", "lr_count_photos", "lr_list_photos", "lr_get_selected", "lr_photo_metadata",
                     "lr_render_preview", "lr_find_photos", "lr_get_collections", "lr_add_keywords", "lr_set_rating",
                     "lr_set_flag", "lr_set_title_caption", "lr_set_gps_metadata", "lr_create_virtual_copy",
                     "lr_apply_develop_settings", "lr_clear_auto_tone", "lr_stage_removal", "lr_call"}


SHAPES = [
    ("lr_count_photos", {}, "catalog.countPhotos", {}),
    ("lr_list_photos", {"limit": 9999, "offset": 5}, "catalog.getAllPhotos", {"limit": 200, "offset": 5}),
    ("lr_get_selected", {}, "catalog.getSelectedPhotos", {}),
    ("lr_photo_metadata", {"photo_id": 1}, "catalog.getPhotoMetadata", {"photoId": 1}),
    ("lr_find_photos", {"search_desc": {"rating": 5}}, "catalog.findPhotos",
     {"searchDesc": {"rating": 5}, "limit": 50, "offset": 0}),
    ("lr_get_collections", {}, "catalog.getCollections", {}),
    ("lr_add_keywords", {"photo_id": 1, "keywords": ["a", "b"]}, "catalog.addKeywords",
     {"photoId": 1, "keywords": ["a", "b"]}),
    ("lr_set_rating", {"photo_id": 1, "rating": 3}, "catalog.setRating", {"photoId": 1, "rating": 3}),
    ("lr_set_flag", {"photo_id": 1, "flag": -1}, "catalog.setFlag", {"photoId": 1, "flag": -1}),
    ("lr_set_gps_metadata", {"photo_id": 1, "latitude": 1.5, "longitude": -2.5}, "catalog.setMetadata",
     {"photoId": 1, "key": "gps", "value": {"latitude": 1.5, "longitude": -2.5}}),
    ("lr_create_virtual_copy", {"photo_id": 1}, "catalog.createVirtualCopy", {"photoId": 1}),
    ("lr_apply_develop_settings", {"photo_id": 2, "settings": {"Exposure2012": 0.5}}, "develop.applySettings",
     {"photoId": 2, "settings": {"Exposure2012": 0.5}}),
    ("lr_clear_auto_tone", {"photo_ids": [1]}, "develop.clearAutoTone", {"photoIds": [1], "dryRun": True}),
    ("lr_clear_auto_tone", {"photo_ids": [1], "dry_run": False}, "develop.clearAutoTone",
     {"photoIds": [1], "dryRun": False}),
    ("lr_stage_removal", {"photo_ids": [1, 2]}, "catalog.removeFromCatalog", {"photoIds": [1, 2]}),
    ("lr_call", {"command": "catalog.getKeywords", "params": {"x": 1}}, "catalog.getKeywords", {"x": 1}),
]


async def test_request_shapes(client, mock):
    for tool, args, cmd, params in SHAPES:
        await client.call_tool(tool, args)
        assert last(mock) == (cmd, params), tool


async def test_title_caption_two_calls(client, mock):
    await client.call_tool("lr_set_title_caption", {"photo_id": 1, "title": "T", "caption": "C"})
    assert mock.received[-2:] == [("catalog.setTitle", {"photoId": 1, "title": "T"}),
                                  ("catalog.setCaption", {"photoId": 1, "caption": "C"})]


async def test_status(client):
    r = await client.call_tool("lr_status", {})
    assert r.data["health"]["bridge"] == "connected" and r.data["ping"]["pong"] is True


async def test_preview_image_cap_and_rate_limit(client, mock):
    r = await client.call_tool("lr_render_preview", {"photo_id": 1, "size": 99999})
    assert last(mock)[0] == "preview.generatePreview" and last(mock)[1]["size"] == 1600
    assert r.content[0].type == "image" and r.content[0].mimeType == "image/jpeg"
    with pytest.raises(ToolError, match="rate limited"):
        await client.call_tool("lr_render_preview", {"photo_id": 1})


async def test_guard_denies_develop_write_on_original(client, mock):
    with pytest.raises(ToolError, match="GUARD_DENIED"):
        await client.call_tool("lr_apply_develop_settings", {"photo_id": 1, "settings": {"Exposure2012": 1}})
    assert not any(c == "develop.applySettings" for c, _ in mock.writes)
    await client.call_tool("lr_apply_develop_settings", {"photo_id": 2, "settings": {"Exposure2012": 1}})  # VC ok
    assert any(c == "develop.applySettings" for c, _ in mock.writes)


async def test_clear_auto_tone_allowed_on_original(client, mock):
    await client.call_tool("lr_clear_auto_tone", {"photo_ids": [1], "dry_run": False})
    assert last(mock)[0] == "develop.clearAutoTone"


REFUSED = ["catalog.setRating", "develop.setValue", "catalog.removeFromCatalog",
                                 "develop.clearAutoTone", "catalog.setSelectedPhotos", "preview.generatePreview",
                                 "catalog.copySettings", "develop.editInPhotoshop", "nope.deleteFile", "bogus"]


async def test_lr_call_refuses_non_read(client, mock):
    n = len(mock.received)
    for cmd in REFUSED:
        with pytest.raises(ToolError, match="refuses"):
            await client.call_tool("lr_call", {"command": cmd, "params": {"photoId": 1}})
    assert len(mock.received) == n


BAD = [
    ("lr_set_rating", {"photo_id": 1, "rating": 9}), ("lr_set_flag", {"photo_id": 1, "flag": 2}),
    ("lr_photo_metadata", {"photo_id": -1}), ("lr_add_keywords", {"photo_id": 1, "keywords": []}),
    ("lr_set_gps_metadata", {"photo_id": 1, "latitude": 99, "longitude": 0}),
    ("lr_clear_auto_tone", {"photo_ids": list(range(1, 52))}), ("lr_stage_removal", {"photo_ids": []}),
]


async def test_bad_args_never_reach_daemon(client, mock):
    n = len(mock.received)
    for tool, args in BAD:
        with pytest.raises(ToolError):
            await client.call_tool(tool, args)
    assert len(mock.received) == n


async def test_stdout_clean_and_no_delete_surface(client):
    for t in await client.list_tools():
        assert "path" not in json.dumps(t.inputSchema) and "delete" not in t.name
