import re
from pathlib import Path

import pytest

from lightroom_sdk.daemon import guard
from lightroom_sdk.daemon.classes import CLASSES

LUA = Path(__file__).resolve().parents[2] / "lightroom_sdk" / "plugin" / "PluginInit.lua"


def lua_names():
    return re.findall(r'router:register\(\s*"([^"]+)"', LUA.read_text(encoding="utf-8", errors="replace"))


def test_classes_match_plugin_register_list_exactly():
    names = lua_names()
    assert len(names) == 132 and len(set(names)) == 132
    assert set(names) == set(CLASSES), (set(names) ^ set(CLASSES))


def test_class_counts():
    from collections import Counter

    c = Counter(CLASSES.values())
    assert c == {"R": 24, "RD": 15, "UI": 16, "LM": 20, "LS": 5, "VC": 1, "DW": 49, "RM": 1, "EXT": 1}


def test_deny_regex_hits_no_known_command():
    assert [n for n in CLASSES if guard.DENY_RE.search(n)] == []


@pytest.mark.parametrize("name", ["develop.deleteFile", "catalog.deletePhotoFromDisk", "catalog.moveToTrash",
                                  "x.unlink", "catalog.eraseOriginal", "catalog.removeFile"])
def test_deny_regex_blocks_disk_delete_names_even_with_flags(name):
    v = guard.precheck(name, {}, {"allow_remove_from_catalog": True, "allow_external_edit": True})
    assert not v.allow and v.klass is None


def test_unknown_name_default_denied():
    assert not guard.precheck("catalog.newThing", {}, {}).allow
    assert not guard.precheck(None, {}, {}).allow
    assert not guard.precheck("cancelCommand", {}, {}).allow  # only via /v1/cancel


def test_every_known_name_classified_per_policy():
    for name, k in CLASSES.items():
        v = guard.precheck(name, {}, {})
        if k in ("RM", "EXT"):
            assert not v.allow, name
        else:
            assert v.allow, name
            assert v.needs_resolve == (k == "DW"), name


def test_flags_unlock_only_their_own_class():
    assert guard.precheck("catalog.removeFromCatalog", {}, {"allow_remove_from_catalog": True}).allow
    assert not guard.precheck("catalog.removeFromCatalog", {}, {"allow_external_edit": True}).allow
    assert not guard.precheck("catalog.removeFromCatalog", {}, {"allow_remove_from_catalog": "yes"}).allow
    assert guard.precheck("develop.editInPhotoshop", {}, {"allow_external_edit": True}).allow
    assert not guard.precheck("develop.editInPhotoshop", {}, None).allow


def test_set_metadata_any_key_allowed_on_originals():
    for key in ("rating", "gps", "city", "someWeirdKey", "path"):
        v = guard.precheck("catalog.setMetadata", {"photoId": 1, "key": key, "value": "x"}, {})
        assert v.allow and not v.needs_resolve


def test_library_writes_on_originals_need_no_resolve():
    for n in ("catalog.addKeywords", "catalog.createCollection", "catalog.setFlag", "catalog.createVirtualCopy",
              "selection.setRating", "catalog.setTitle", "catalog.setCaption"):
        v = guard.precheck(n, {"photoId": 1}, {})
        assert v.allow and not v.needs_resolve


async def _resolve(params, replies):
    async def call(c, p):
        return replies(c, p)

    return await guard.resolve_and_check(params, call)


async def test_resolve_explicit_ids_and_selection_and_missing_flag():
    meta = lambda c, p: {"success": True, "result": {"isVirtualCopy": p["photoId"] in (2, 3)}}
    assert (await _resolve({"photoId": 2}, meta))[0] is True
    assert (await _resolve({"photoIds": [2, 3]}, meta))[0] is True
    ok, aff, det = await _resolve({"photoIds": [2, 1]}, meta)
    assert not ok and det["offending"] == [1]
    sel = lambda c, p: {"success": True, "result": {"photos": [{"id": 5, "isVirtualCopy": True}]}}
    assert (await _resolve({}, sel))[0] is True
    # absent isVirtualCopy (Lua omits nil) is not "true"
    nokey = lambda c, p: {"success": True, "result": {"photos": [{"id": 5}]}}
    assert (await _resolve({}, nokey))[0] is False
    assert (await _resolve({}, lambda c, p: {"success": True, "result": {"photos": []}}))[0] is False
    assert (await _resolve({}, lambda c, p: {"success": False, "error": {"code": "X"}}))[0] is False
