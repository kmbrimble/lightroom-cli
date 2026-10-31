"""lr-mcp-proxy: stdio MCP server that is a thin proxy to the lrd DAEMON only (never the plugin bridge).

Every tool is one POST /v1/call to the daemon, so the daemon's guard, scheduler and audit log apply. The proxy
adds no policy beyond: a fixed tool set, lr_call limited to read commands, and a size cap + rate limit on previews.
stdout carries only MCP JSON-RPC: nothing here prints; logs go to stderr.

Daemon address/token: same as lrc.py (LRD_HOME or %LOCALAPPDATA%\\lightroom-cli, file lrd.json).
Env: LRD_HOME, LR_PROXY_PREVIEW_MIN_INTERVAL (seconds between previews, default 2).
"""
import asyncio
import base64
import json
import os
import sys
import time

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

from . import lrc

CLIENT = "mcp-proxy"
MAX_PREVIEW = 1600
MAX_PAGE = 200
# R = pure catalog reads. RD = Develop-controller reads (need the Develop module active, exclusive lane).
LR_CALL_CLASSES = frozenset({"R", "RD"})
# Read-class names lr_call still refuses: huge payloads (use lr_render_preview) or probes with side effects.
LR_CALL_DENY = frozenset({
    "preview.generatePreview", "preview.generateBatchPreviews", "preview.getPreviewChunk",
    "develop.monitorParameterChanges", "develop.probeAllDevelopParameters", "develop.discoverGradientParameters",
    "develop.dumpLrDevelopController", "catalog.copySettings",
})

TRAPS = (
    " Rules: one Lightroom operation at a time (never fire calls in parallel); ids are Lightroom LOCAL ids"
    " (integers from these tools, not file names); stacks are collapsed, so a stack shows only its top photo;"
    " do not rely on selection (setSelectedPhotos is unreliable): always pass explicit photo ids."
)

mcp = FastMCP(
    "lightroom",
    instructions="Controls Adobe Lightroom Classic through the lrd daemon (guarded, audited)." + TRAPS
    + " Develop edits are only accepted on VIRTUAL COPIES; create one first with lr_create_virtual_copy.",
)

_classes = None
_last_preview = 0.0


async def _http(method, path, body=None, timeout=30):
    _, obj = await asyncio.to_thread(lrc.request, method, path, body, timeout)
    return obj


async def _call(command, params=None, flags=None, timeout=None):
    body = {"command": command, "params": params or {}, "client": CLIENT}
    if flags:
        body["flags"] = flags
    if timeout:
        body["timeout"] = timeout
    obj = await _http("POST", "/v1/call", body, (timeout or 120) + 15)
    if not obj.get("ok"):
        raise ToolError(json.dumps(obj.get("error") or obj))
    return obj.get("result")


def _ids(photo_ids):
    if not isinstance(photo_ids, list) or not photo_ids or not all(type(i) is int and i > 0 for i in photo_ids):
        raise ToolError("photo_ids must be a non-empty list of positive integer Lightroom local ids")
    return photo_ids


@mcp.tool
async def lr_status() -> dict:
    """Daemon health (bridge connected/down, queue, uptime) plus a plugin ping. Call first if anything fails."""
    health = await _http("GET", "/v1/health", None, 5)
    out = {"health": health}
    if health.get("ok") and health.get("bridge") == "connected":
        out["ping"] = await _call("system.ping")
    return out


@mcp.tool
async def lr_count_photos() -> dict:
    """Total number of photos in the catalog."""
    return await _call("catalog.countPhotos")


@mcp.tool
async def lr_list_photos(limit: int = 50, offset: int = 0) -> dict:
    """List catalog photos (paged, max 200 per page; use offset for the next page)."""
    return await _call("catalog.getAllPhotos", {"limit": max(1, min(int(limit), MAX_PAGE)), "offset": max(0, int(offset))})


@mcp.tool
async def lr_get_selected() -> dict:
    """Photos currently selected in Lightroom (read-only; selection cannot be set reliably, so prefer explicit ids)."""
    return await _call("catalog.getSelectedPhotos")


@mcp.tool
async def lr_photo_metadata(photo_id: int) -> dict:
    """Metadata of one photo by Lightroom local id (rating, flag, keywords, title, isVirtualCopy, ...)."""
    return await _call("catalog.getPhotoMetadata", {"photoId": _ids([photo_id])[0]})


@mcp.tool
async def lr_render_preview(photo_id: int, size: int = 1024) -> list:
    """Render a JPEG preview of one photo as an image. size is the long edge in pixels, capped at 1600. Rate-limited
    (a few seconds between calls); one preview at a time."""
    global _last_preview
    gap = float(os.environ.get("LR_PROXY_PREVIEW_MIN_INTERVAL", "2"))
    wait = _last_preview + gap - time.monotonic()
    if wait > 0:
        raise ToolError(f"rate limited: previews are limited to one per {gap:g}s; retry in {wait:.1f}s")
    _last_preview = time.monotonic()
    size = max(64, min(int(size), MAX_PREVIEW))
    r = await _call("preview.generatePreview", {"photoId": _ids([photo_id])[0], "size": size, "quality": 80,
                                                 "format": "jpeg", "base64": True}, timeout=110)
    data = (r or {}).get("preview")
    if not isinstance(data, str) or data == "CHUNKED_TRANSFER":
        raise ToolError("preview too large or missing; retry with a smaller size")
    return [Image(data=base64.b64decode(data), format="jpeg").to_image_content(),
            json.dumps((r or {}).get("info") or {})]


@mcp.tool
async def lr_find_photos(search_desc: dict, limit: int = 50, offset: int = 0) -> dict:
    """Find photos with a Lightroom searchDesc filter dict (e.g. {"rating": 5}, {"keywords": "x"}); paged, max 200."""
    return await _call("catalog.findPhotos", {"searchDesc": search_desc,
                                              "limit": max(1, min(int(limit), MAX_PAGE)), "offset": max(0, int(offset))})


@mcp.tool
async def lr_get_collections() -> dict:
    """List collections and collection sets."""
    return await _call("catalog.getCollections")


@mcp.tool
async def lr_add_keywords(photo_id: int, keywords: list[str]) -> dict:
    """Add keywords to one photo (creates missing keywords; never removes existing ones). There is no 'set keywords'
    (replace) command in the plugin."""
    if not keywords or not all(isinstance(k, str) and k.strip() for k in keywords):
        raise ToolError("keywords must be a non-empty list of non-empty strings")
    return await _call("catalog.addKeywords", {"photoId": _ids([photo_id])[0], "keywords": keywords})


@mcp.tool
async def lr_set_rating(photo_id: int, rating: int) -> dict:
    """Set the star rating (0-5; 0 = unrated) of one photo."""
    if not 0 <= rating <= 5:
        raise ToolError("rating must be 0-5")
    return await _call("catalog.setRating", {"photoId": _ids([photo_id])[0], "rating": rating})


@mcp.tool
async def lr_set_flag(photo_id: int, flag: int) -> dict:
    """Set the pick flag of one photo: 1 = pick, -1 = reject, 0 = none."""
    if flag not in (-1, 0, 1):
        raise ToolError("flag must be 1, -1 or 0")
    return await _call("catalog.setFlag", {"photoId": _ids([photo_id])[0], "flag": flag})


@mcp.tool
async def lr_set_title_caption(photo_id: int, title: str | None = None, caption: str | None = None) -> dict:
    """Set the title and/or caption of one photo (omit a field to leave it unchanged)."""
    if title is None and caption is None:
        raise ToolError("give title and/or caption")
    pid, out = _ids([photo_id])[0], {}
    if title is not None:
        out["title"] = await _call("catalog.setTitle", {"photoId": pid, "title": title})
    if caption is not None:
        out["caption"] = await _call("catalog.setCaption", {"photoId": pid, "caption": caption})
    return out


@mcp.tool
async def lr_set_gps_metadata(photo_id: int, latitude: float, longitude: float) -> dict:
    """Set the GPS location of one photo (decimal degrees). Uses the generic catalog.setMetadata('gps') path."""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ToolError("latitude must be -90..90 and longitude -180..180")
    return await _call("catalog.setMetadata", {"photoId": _ids([photo_id])[0], "key": "gps",
                                               "value": {"latitude": latitude, "longitude": longitude}})


@mcp.tool
async def lr_create_virtual_copy(photo_id: int) -> dict:
    """Create a virtual copy of one photo (the only kind of photo Develop edits are allowed on). Returns the new photo
    id. The new copy becomes Lightroom's selection."""
    return await _call("catalog.createVirtualCopy", {"photoId": _ids([photo_id])[0]})


@mcp.tool
async def lr_apply_develop_settings(photo_id: int, settings: dict) -> dict:
    """Apply Develop settings (e.g. {"Exposure2012": 0.5}) to one photo. The daemon only permits this on VIRTUAL COPIES
    and answers GUARD_DENIED for originals: create a copy with lr_create_virtual_copy first."""
    return await _call("develop.applySettings", {"photoId": _ids([photo_id])[0], "settings": settings})


@mcp.tool
async def lr_clear_auto_tone(photo_ids: list[int], dry_run: bool = True) -> dict:
    """Remove Lightroom's Auto Tone (fixed tone keys back to 0, Auto flags off) from up to 50 photos, originals
    included. dry_run=true (default) only reports what would change; pass dry_run=false to apply."""
    ids = _ids(photo_ids)
    if len(ids) > 50:
        raise ToolError("at most 50 photo ids per call")
    return await _call("develop.clearAutoTone", {"photoIds": ids, "dryRun": bool(dry_run)})


@mcp.tool
async def lr_stage_removal(photo_ids: list[int]) -> dict:
    """STAGE photos for removal: puts them in the collection _LRCLI_TO_REMOVE and shows it. Nothing is removed or
    deleted; the actual 'Remove from Catalog' is a manual step the user does in the Lightroom UI. Files on disk are
    never touched."""
    return await _call("catalog.removeFromCatalog", {"photoIds": _ids(photo_ids)}, flags={"allow_remove_from_catalog": True})


@mcp.tool
async def lr_call(command: str, params: dict | None = None) -> dict:
    """Generic READ-ONLY call: only daemon command classes R/RD (catalog/selection/develop getters) are allowed; anything
    that writes is refused. Preview commands are excluded (use lr_render_preview). Example: command='catalog.getKeywords'."""
    global _classes
    if _classes is None:
        obj = await _http("GET", "/v1/commands", None, 5)
        if not obj.get("ok"):
            raise ToolError(json.dumps(obj.get("error") or obj))
        _classes = obj["commands"]
    k = _classes.get(command)
    if k not in LR_CALL_CLASSES or command in LR_CALL_DENY:
        raise ToolError(f"lr_call refuses {command!r} (class {k}): only read commands are allowed; use the dedicated lr_* tool")
    return await _call(command, params or {})


def main():
    # Keep stdout clean for JSON-RPC: no banner; any stray library print would corrupt the stream, so send it to stderr.
    sys.stdout.reconfigure(write_through=True)
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
