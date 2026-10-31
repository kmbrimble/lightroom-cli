"""develop.clearAutoTone: resets exactly the Auto Tone keys, on originals and virtual copies."""
import pytest

SLIDERS = ["Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012",
           "Whites2012", "Blacks2012", "Vibrance", "Saturation"]

# Resolved Auto Tone state + a lot of keys that must NOT be touched.
SETUP = """
AT = require 'AutoToneModule'
function resolved() return {
  Exposure2012 = 0.41, Contrast2012 = 5, Highlights2012 = -28, Shadows2012 = 56, Whites2012 = 27,
  Blacks2012 = -26, Vibrance = 13, Saturation = -1,
  CropLeft = 0.0062, CropRight = 0.9938, HDREditMode = 1, CameraProfile = 'Adobe Standard',
  LensProfileEnable = 1, Temperature = 5300, Tint = 16, ProcessVersion = 15.4, Sharpness = 40,
  Clarity2012 = 7, Texture = 3, Dehaze = 2, AutoToneDigest = 'ABC', ToneCurveName2012 = 'Linear' } end
function pending() return {
  Exposure2012 = -999999, Contrast2012 = -999999, Highlights2012 = -999999, Shadows2012 = -999999,
  Whites2012 = -999999, Blacks2012 = -999999, Vibrance = -999999, Saturation = -999999,
  AutoTone = true, AutoToneDigest = -999999, FillLight = -999999, CropLeft = 0.003 } end
T.addPhoto(1, false, resolved())   -- original, resolved values
T.addPhoto(2, true, resolved())    -- virtual copy
T.addPhoto(3, false, pending())    -- original, placeholders
T.addPhoto(9, false, {}, 'VIDEO')
function call(p) local r; AT.clearAutoTone(p, function(x) r = x end); return r end
"""

OTHER = ["CropLeft", "CropRight", "HDREditMode", "CameraProfile", "LensProfileEnable", "Temperature",
         "Tint", "ProcessVersion", "Sharpness", "Clarity2012", "Texture", "Dehaze", "AutoToneDigest",
         "ToneCurveName2012"]


def keys(env, expr):
    return set(str(k) for k in env.run(f"local t = {{}}; for k in pairs({expr}) do t[#t+1] = k end; return table.concat(t, ',')").split(","))


def test_resets_exactly_the_slider_keys_and_nothing_else(env):
    env.run(SETUP)
    r = env.run("""
        local before = {}; for k, v in pairs(T.photos[1].dev) do before[k] = v end
        local resp = call({ photoIds = { 1 } })
        local diff = {}
        for k, v in pairs(T.photos[1].dev) do if before[k] ~= v then diff[#diff+1] = k end end
        table.sort(diff)
        local e = resp.result.photos[1]
        return table.concat(diff, ','), e.changed.Exposure2012.before, e.changed.Exposure2012.after, e.filename, #T.writes, T.writes[1].name, T.writes[1].flatten == nil
    """)
    diff, eb, ea, fname, nw, hname, noflat = r
    assert diff == ",".join(sorted(SLIDERS))
    assert (eb, ea, fname, nw, hname, noflat) == (0.41, 0, "photo1.dng", 1, "Clear Auto Tone", True)


def test_works_on_original_virtual_copy_and_placeholder(env):
    env.run(SETUP)
    r = env.run("""
        local resp = call({ photoIds = { 1, 2, 3 } })
        local p = resp.result.photos
        local P3 = T.photos[3].dev
        return resp.result.count, T.photos[1].dev.Exposure2012, T.photos[2].dev.Whites2012, P3.Exposure2012,
               P3.Saturation, P3.AutoTone, P3.AutoToneDigest, P3.FillLight, P3.CropLeft, T.lastGate,
               p[3].changed.Exposure2012.before, p[3].changed.AutoTone.after
    """)
    assert tuple(r) == (3, 0, 0, 0, 0, False, -999999, -999999, 0.003, "Clear Auto Tone", -999999, False)


def test_one_write_gate_and_idempotent(env):
    env.run(SETUP)
    r = env.run("""
        call({ photoIds = { 1, 2, 3 } })
        local n1 = #T.writes
        local resp = call({ photoIds = { 1, 2, 3 } })
        local e = resp.result.photos[1]
        return n1, #T.writes, next(e.changed) == nil, e.skippedReason
    """)
    assert tuple(r) == (4, 4, True, "nothing to change")  # photo 3: sliders + marker = 2 writes


def test_dry_run_writes_nothing_but_reports(env):
    env.run(SETUP)
    r = env.run("""
        local resp = call({ photoIds = { 1, 3 }, dryRun = true })
        local e = resp.result.photos[1]
        return resp.result.dryRun, #T.writes, T.photos[1].dev.Exposure2012, e.changed.Shadows2012.before,
               e.changed.Shadows2012.after, T.lastGate == nil, resp.result.photos[2].changed.AutoTone.before
    """)
    assert tuple(r) == (True, 0, 0.41, 56, 0, True, True)


def test_more_than_50_ids_rejected_exactly_50_ok(env):
    env.run(SETUP)
    r = env.run("""
        local ids = {}; for i = 1, 51 do ids[i] = 1 end
        local bad = call({ photoIds = ids })
        local ids50 = {}; for i = 1, 50 do ids50[i] = 1 end
        local good = call({ photoIds = ids50, dryRun = true })
        return bad.error.code, #T.writes, good.error == nil
    """)
    assert tuple(r) == ("INVALID_PARAM_VALUE", 0, True)


@pytest.mark.parametrize("params,code", [
    ("{}", "INVALID_PARAM_VALUE"), ("{ photoIds = {} }", "INVALID_PARAM_VALUE"),
    ("{ photoIds = { 'x' } }", "INVALID_PARAM_VALUE"), ("{ photoIds = { 1, 777 } }", "PHOTO_NOT_FOUND"),
    ("{ photoIds = { 1, 9 } }", "NOT_AN_IMAGE"),
])
def test_bad_input_replies_error_and_writes_nothing(env, params, code):
    env.run(SETUP)
    r = env.run(f"local resp = call({params}); return resp.error.code, #T.writes")
    assert tuple(r) == (code, 0)


def test_gate_abort_replies_write_aborted(env):
    env.run(SETUP)
    r = env.run("""
        T.gate = 'aborted'
        local resp = call({ photoIds = { 1 } })
        return resp.error.code, #T.writes, T.photos[1].dev.Exposure2012
    """)
    assert tuple(r) == ("WRITE_ABORTED", 0, 0.41)


def test_apply_failure_is_per_photo_not_batch(env):
    env.run(SETUP)
    r = env.run("""
        T.applyFails = true
        local resp = call({ photoIds = { 1, 2 } })
        return resp.error == nil, #resp.result.photos, resp.result.photos[1].skippedReason ~= nil, next(resp.result.photos[1].changed) == nil
    """)
    assert tuple(r) == (True, 2, True, True)


def test_never_writes_a_key_outside_the_whitelist(env):
    env.run(SETUP)
    r = env.run("""
        call({ photoIds = { 1, 2, 3 }, settings = { Exposure2012 = 9, CropLeft = 1 }, extra = true })
        local bad = {}
        local allowed = {}
        for _, k in ipairs(AT.SLIDER_KEYS) do allowed[k] = true end
        for _, k in ipairs(AT.MARKER_KEYS) do allowed[k] = true end
        for _, w in ipairs(T.writes) do for k, v in pairs(w.keys) do
            if not allowed[k] or (v ~= 0 and v ~= false) then bad[#bad+1] = k end end end
        return #bad, T.photos[1].dev.CropLeft
    """)
    assert tuple(r) == (0, 0.0062)


def test_other_dw_commands_still_denied_on_original(env):
    env.run(SETUP)
    r = env.run("""
        local Guard = require 'Guard'
        T.select(T.photos[1])
        local denied = 0
        for _, name in ipairs(Guard.DW_COMMANDS) do
            local resp
            local h = Guard.wrapIfDW(name, function(p, cb) cb({ result = {} }) end)
            h({ photoId = 1 }, function(x) resp = x end)
            if resp.error and resp.error.code == 'GUARD_DENIED' then denied = denied + 1 end
        end
        return denied, #Guard.DW_COMMANDS, Guard.isDW('develop.clearAutoTone')
    """)
    assert tuple(r) == (49, 49, False)
