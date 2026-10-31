"""Write-gate aborts must produce WRITE_ABORTED instead of silence."""
import pytest

HANDLERS = [
    ("setRating", "{ photoId = 1, rating = 3 }"),
    ("addKeywords", "{ photoId = 1, keywords = { 'a' } }"),
    ("setFlag", "{ photoId = 1, flag = 1 }"),
    ("setTitle", "{ photoId = 1, title = 't' }"),
    ("setCaption", "{ photoId = 1, caption = 'c' }"),
    ("setColorLabel", "{ photoId = 1, label = 'red' }"),
    ("createVirtualCopy", "{}"),
    ("setMetadata", "{ photoId = 1, key = 'k', value = 'v' }"),
    ("createCollection", "{ name = 'c' }"),
    ("createSmartCollection", "{ name = 'c', searchDesc = {} }"),
    ("createCollectionSet", "{ name = 'c' }"),
    ("createKeyword", "{ keyword = 'k' }"),
    ("removeKeyword", "{ photoId = 1, keyword = 'k' }"),
    ("removeFromCatalog", "{ photoId = 1 }"),
    ("setSelectedPhotos", "{ photoIds = { 1 } }"),
    ("applyDevelopPreset", "{ presetName = 'p' }"),
    ("rotateLeft", "{}"),
    ("rotateRight", "{}"),
    ("pasteSettings", "{}"),
    ("createDevelopSnapshot", "{ name = 's' }"),
]


@pytest.mark.parametrize("name,params", HANDLERS)
def test_aborted_gate_returns_error(env, name, params):
    r = env.run(f"""
        T.addPhoto(1, true); T.select(T.photos[1])
        T.gate = 'aborted'
        local CM = require 'CatalogModule'
        local n, resp = 0, nil
        CM.{name}({params}, function(x) n = n + 1; resp = x end)
        return n, resp.error and resp.error.code
    """)
    assert tuple(r) == (1, "WRITE_ABORTED"), name


def test_executed_gate_still_succeeds(env):
    r = env.run("""
        T.addPhoto(1, true); T.select(T.photos[1])
        local CM = require 'CatalogModule'
        local n, resp = 0, nil
        CM.setMetadata({ photoId = 1, key = 'k', value = 'v' }, function(x) n = n + 1; resp = x end)
        return n, resp.error == nil
    """)
    assert tuple(r) == (1, True)


def test_gate_helper_unit(env):
    r = env.run("""
        local WG = require 'WriteGate'
        local ok, err = pcall(WG.run, T.catalog, 'x', function() end, {})
        T.gate = 'aborted'
        local ok2, err2 = pcall(WG.run, T.catalog, 'x', function() end, {})
        T.gate = 'queued'
        local ok3 = pcall(WG.run, T.catalog, 'x', function() end, {})
        return ok, ok2, ok3, WG.codeFor(err2, 'D'), WG.codeFor('boom', 'D'), WG.codeFor('GUARD_DENIED: x', 'D')
    """)
    assert tuple(r) == (True, False, False, "WRITE_ABORTED", "D", "GUARD_DENIED")
