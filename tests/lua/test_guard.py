"""Virtual-copy guard: Develop writes only on virtual copies (offline, real Lua 5.1)."""
from pathlib import Path

from .conftest import PLUGIN_DIR

SETUP = """
local Guard = require 'Guard'
T.addPhoto(1, false)  -- original
T.addPhoto(2, true)   -- virtual copy
T.addPhoto(3, false)  -- original
T.addPhoto(4, true)   -- virtual copy
"""


def test_wrap_allows_virtual_copy(env):
    r = env.run(SETUP + """
        T.select(T.photos[2])
        local called, resp = 0, nil
        local h = Guard.wrap('develop.setValue', function(p, cb) called = called + 1; cb({ result = {} }) end)
        h({ photoId = 2 }, function(x) resp = x end)
        return called, resp.error == nil
    """)
    assert tuple(r) == (1, True)


def test_wrap_denies_original_target(env):
    r = env.run(SETUP + """
        T.select(T.photos[1])
        local called, resp = 0, nil
        local h = Guard.wrap('develop.setValue', function(p, cb) called = called + 1; cb({ result = {} }) end)
        h({}, function(x) resp = x end)
        return called, resp.error.code, resp.error.message, resp.error.details.photoIds[1]
    """)
    called, code, msg, bad = r
    assert called == 0
    assert code == "GUARD_DENIED"
    assert "catalog.createVirtualCopy" in msg and "1" in msg
    assert bad == "1"


def test_wrap_denies_named_original_even_if_target_is_vc(env):
    r = env.run(SETUP + """
        T.select(T.photos[2])
        local called, resp = 0, nil
        local h = Guard.wrap('develop.applySettings', function(p, cb) called = called + 1 end)
        h({ photoId = 3 }, function(x) resp = x end)
        return called, resp.error.code, resp.error.details.photoIds[1]
    """)
    assert tuple(r) == (0, "GUARD_DENIED", "3")


def test_batch_denied_lists_every_offender_and_does_nothing(env):
    r = env.run(SETUP + """
        T.select(T.photos[2])
        local called, resp = 0, nil
        local h = Guard.wrap('develop.batchSetValue', function(p, cb) called = called + 1 end)
        h({ photoIds = { 2, 1, 4, 3, 999 } }, function(x) resp = x end)
        local ids = resp.error.details.photoIds
        return called, #ids, ids[1], ids[2], #T.writes, T.target.localIdentifier
    """)
    assert tuple(r) == (0, 2, "1", "3", 0, 2)


def test_batch_all_virtual_copies_runs(env):
    r = env.run(SETUP + """
        T.select(T.photos[2])
        local called = 0
        local h = Guard.wrap('develop.batchSetValue', function(p, cb) called = called + 1 end)
        h({ photoIds = { 2, 4 } }, function() end)
        return called
    """)
    assert r == 1


def test_backstop_batch_selection_change_mid_batch(env):
    """Real DevelopModule.batchSetValue, UNwrapped: the controller proxy is the backstop.
    Sleep #1 follows selecting photo 2; sleep #2 (after selecting photo 4) is where the
    user/Lightroom switches the selection to an original. The write for 4 must be refused."""
    r = env.run(SETUP + """
        T.select(T.photos[2])
        T.sleepHook = function(n) if n == 2 then T.select(T.photos[1]) end end
        local DM = require 'DevelopModule'
        local resp
        DM.batchSetValue({ photoIds = { 2, 4 }, param = 'Exposure', value = 1 }, function(x) resp = x end)
        local res = resp.result.results
        return #T.writes, T.writes[1].target, resp.result.succeeded, res[1].success, res[2].success,
               string.find(res[2].error, 'GUARD_DENIED', 1, true) ~= nil
    """)
    assert tuple(r) == (1, 2, 1, True, False, True)


def test_backstop_batch_apply_settings_denies_original_without_wrapper(env):
    r = env.run(SETUP + """
        T.select(T.photos[2])
        local DM = require 'DevelopModule'
        local resp
        DM.batchApplySettings({ photoIds = { 3 }, settings = { Exposure = 1, Contrast = 2 } }, function(x) resp = x end)
        return #T.writes, resp.result.succeeded
    """)
    assert tuple(r) == (0, 0)


def test_controller_reads_and_ui_not_blocked_writes_blocked(env):
    r = env.run(SETUP + """
        T.select(T.photos[1])   -- original selected
        local c = Guard.guardController(T.ctl)
        local a = c.getValue('Exposure')
        local lo, hi = c.getRange('Exposure')
        c.selectTool('masking'); c.goToMasking(); c.toggleOverlay(); c.getProcessVersion()
        local w = {}
        for _, n in ipairs({ 'setValue', 'resetToDefault', 'createNewMask', 'addToCurrentMask',
                             'deleteMask', 'resetCrop', 'someFutureWrite' }) do
            local ok, err = pcall(c[n], 'x', 1)
            w[#w + 1] = tostring((not ok) and string.find(err, 'GUARD_DENIED', 1, true) ~= nil)
        end
        return a, hi, #T.writes, table.concat(w, ','), c.CONSTANT
    """)
    a, hi, nwrites, denied, const = r
    assert (a, hi, nwrites, const) == (0, 5, 0, 42)
    assert denied == ",".join(["true"] * 7)


def test_controller_write_allowed_on_vc_and_checked_at_write_time(env):
    r = env.run(SETUP + """
        T.select(T.photos[2])
        local c = Guard.guardController(T.ctl)
        c.setValue('Exposure', 1)
        T.select(T.photos[1])                      -- selection changes between calls
        local ok = pcall(c.setValue, 'Exposure', 2)
        return #T.writes, T.writes[1].target, ok
    """)
    assert tuple(r) == (1, 2, False)


def test_no_target_photo_fails_closed(env):
    r = env.run(SETUP + """
        T.target = nil
        local c = Guard.guardController(T.ctl)
        local ok, err = pcall(c.setValue, 'Exposure', 1)
        return ok, #T.writes
    """)
    assert tuple(r) == (False, 0)


def test_catalog_develop_writes_denied_on_original_allowed_on_vc(env):
    r = env.run(SETUP + """
        local CM = require 'CatalogModule'
        local out = {}
        for _, n in ipairs({ 'rotateLeft', 'rotateRight', 'pasteSettings' }) do
            T.select(T.photos[1])
            local resp
            CM[n]({}, function(x) resp = x end)            -- unwrapped: in-gate assertVirtualCopy
            out[#out + 1] = resp.error.code
            T.select(T.photos[2])
            CM[n]({}, function(x) resp = x end)
            out[#out + 1] = tostring(resp.error == nil)
        end
        T.select(T.photos[1]); local resp
        CM.applyDevelopPreset({ presetName = 'p' }, function(x) out[#out + 1] = x.error.code end)
        CM.createDevelopSnapshot({ name = 's' }, function(x) resp = x end)
        out[#out + 1] = resp.error.code
        return table.concat(out, ','), #T.writes
    """)
    s, nwrites = r
    assert s == "GUARD_DENIED,true," * 3 + "GUARD_DENIED,GUARD_DENIED"
    assert nwrites == 3  # only the three VC writes happened


def test_library_writes_and_vc_creation_unrestricted_on_originals(env):
    r = env.run(SETUP + """
        local CM = require 'CatalogModule'
        T.select(T.photos[1])
        local out = {}
        local function run(name, p) local resp; CM[name](p, function(x) resp = x end); return resp end
        out[#out+1] = tostring(run('setRating', { photoId = 1, rating = 3 }).error == nil)
        out[#out+1] = tostring(run('setTitle', { photoId = 1, title = 't' }).error == nil)
        out[#out+1] = tostring(run('createVirtualCopy', {}).error == nil)
        out[#out+1] = tostring(run('createKeyword', { keyword = 'k' }).error == nil)
        return table.concat(out, ',')
    """)
    assert r == "true,true,true,true"


def test_guard_pref_disables_and_defaults_on(env):
    r = env.run(SETUP + """
        T.select(T.photos[1])
        local c = Guard.guardController(T.ctl)
        local before = Guard.isEnabled()
        local default_set = T.prefs.guardEnabled
        T.prefs.guardEnabled = false
        c.setValue('Exposure', 1)
        return before, default_set, #T.writes
    """)
    assert tuple(r) == (True, True, 1)


def test_guard_pref_not_reachable_from_bridge_commands():
    """No plugin file other than Guard.lua may reference guardEnabled, and only Config.lua
    (which exposes no command) and Guard.lua may touch LrPrefs."""
    hits = {p.name for p in PLUGIN_DIR.glob("*.lua") if "guardEnabled" in p.read_text(errors="ignore")}
    assert hits == {"Guard.lua"}
    prefs = {p.name for p in PLUGIN_DIR.glob("*.lua") if "LrPrefs" in p.read_text(errors="ignore")}
    assert prefs == {"Config.lua", "Guard.lua"}
    for p in PLUGIN_DIR.glob("*.lua"):
        assert "Config:set" not in p.read_text(errors="ignore").replace("function Config:set", ""), p.name


def test_batch_on_vcs_runs_even_if_an_original_is_selected(env):
    r = env.run(SETUP + """
        T.select(T.photos[1])          -- an original happens to be selected
        local called = 0
        local h = Guard.wrap('develop.batchSetValue', function(p, cb) called = called + 1 end)
        h({ photoIds = { 2, 4 } }, function() end)
        -- ...but the same selection blocks a single-photo write (writes go to the target)
        local resp
        local h2 = Guard.wrap('develop.setValue', function(p, cb) called = called + 10 end)
        h2({ photoId = 2 }, function(x) resp = x end)
        return called, resp.error.details.photoIds[1]
    """)
    assert tuple(r) == (1, "1")


def test_backstop_original_selected_batch_on_vcs_writes_only_vcs(env):
    r = env.run(SETUP + """
        T.select(T.photos[1])
        local DM = require 'DevelopModule'
        local resp
        DM.batchSetValue({ photoIds = { 2, 4 }, param = 'Exposure', value = 1 }, function(x) resp = x end)
        return #T.writes, T.writes[1].target, T.writes[2].target, resp.result.succeeded
    """)
    assert tuple(r) == (2, 2, 4, 2)


def test_controller_proxy_works_when_namespace_not_enumerable(env):
    r = env.run(SETUP + """
        local real = T.ctl
        local hidden = setmetatable({}, { __index = real })   -- pairs() sees nothing
        T.select(T.photos[1])
        local c = Guard.guardController(hidden)
        local v = c.getValue('Exposure')
        local ok = pcall(c.setValue, 'Exposure', 1)
        T.select(T.photos[2])
        local ok2 = pcall(c.setValue, 'Exposure', 1)
        return v, ok, ok2, #T.writes
    """)
    assert tuple(r) == (0, False, True, 1)
