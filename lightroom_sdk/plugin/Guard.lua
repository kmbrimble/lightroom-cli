-- Guard.lua
-- Safety policy: Develop-module writes are allowed ONLY on virtual copies.
-- Everything else (keywords, collections, flags, ratings, metadata, creating
-- virtual copies) is deliberately NOT restricted here.
--
-- Three layers, all synchronous (no yield between a check and the write it
-- protects; Lightroom Lua tasks are cooperative, so that is atomic):
--   1. Guard.wrapIfDW / Guard.routerProxy: pre-check at command registration
--      (friendly GUARD_DENIED reply listing the offending photo ids).
--   2. Guard.guardController: a copy of LrDevelopController whose write
--      functions re-check the current target photo immediately before the real
--      call. This is the backstop for handlers that yield (sleep, write-gate
--      waits, per-photo setSelectedPhotos in the batch handlers).
--   3. Guard.assertVirtualCopy(photo): called inside catalog-side Develop
--      writes (preset, snapshot, paste, rotate) as the first statement of the
--      write-gate function.
-- The pref `guardEnabled` (LrPrefs, default true) is read directly from
-- LrPrefs; no bridge command writes it.

local LrTasks = import 'LrTasks'
-- Photo metadata calls (getRawMetadata) can yield; a plain pcall cannot yield across them and then
-- reports failure, which would make every virtual copy look like an original (verified live).
local pcall = LrTasks.pcall

local Guard = {}

Guard.CODE = "GUARD_DENIED"
Guard.HINT = "create a virtual copy with catalog.createVirtualCopy, then select it"

-- Develop-write (class DW) commands. Keep in sync with PluginInit.lua; the
-- test tests/lua/test_guard_registration.py fails on drift.
Guard.DW_COMMANDS = {
    "develop.applySettings", "develop.batchApplySettings", "develop.batchSetValue",
    "develop.setValue", "develop.resetToDefault", "develop.setAutoTone",
    "develop.setAutoWhiteBalance", "develop.setProcessVersion",
    "develop.resetAllDevelopAdjustments", "develop.setCurvePoints",
    "develop.setCurveLinear", "develop.setCurveSCurve", "develop.addCurvePoint",
    "develop.removeCurvePoint", "develop.createGreenSwatch", "develop.createCyanSwatch",
    "develop.enhanceColors", "develop.createNewMask", "develop.deleteMask",
    "develop.deleteMaskTool", "develop.addToCurrentMask", "develop.intersectWithCurrentMask",
    "develop.subtractFromCurrentMask", "develop.invertMask", "develop.resetGradient",
    "develop.resetCircularGradient", "develop.resetBrushing", "develop.resetMasking",
    "develop.createGraduatedFilter", "develop.createRadialFilter",
    "develop.createAdjustmentBrush", "develop.createAISelectionMask",
    "develop.createAIMaskWithAdjustments", "develop.batchAIMask", "develop.createRangeMask",
    "develop.createComplexMask", "develop.setLocalValue", "develop.applyLocalSettings",
    "develop.createMaskWithLocalAdjustments", "develop.resetCrop", "develop.resetTransforms",
    "develop.resetSpotRemoval", "develop.resetRedeye", "develop.resetHealing",
    "catalog.applyDevelopPreset", "catalog.createDevelopSnapshot", "catalog.pasteSettings",
    "catalog.rotateLeft", "catalog.rotateRight",
}

-- Class DR (develop reset) commands: the ONLY Develop writes allowed on originals.
-- Explicit allow-list, not a flag. User-authorised 2026-10-31 to remove Bracket
-- Stacker's Auto Tone from every photo. Each entry must be a fixed-key, reset-to-
-- default command (see AutoToneModule.lua); never add a general writer here.
-- These are intentionally NOT in DW_COMMANDS (so they are not wrapped) and must
-- never overlap with it (asserted below).
Guard.DR_COMMANDS = { "develop.clearAutoTone" }

local dwSet = {}
for _, n in ipairs(Guard.DW_COMMANDS) do dwSet[n] = true end
for _, n in ipairs(Guard.DR_COMMANDS) do
    assert(not dwSet[n], "Guard: " .. n .. " cannot be both DW and DR")
end

function Guard.isDW(name) return dwSet[name] == true end

-- ---------------------------------------------------------------- policy pref
function Guard.isEnabled()
    local ok, prefs = pcall(function() return import('LrPrefs').prefsForPlugin() end)
    if ok and prefs then
        if prefs.guardEnabled == nil then prefs.guardEnabled = true end
        return prefs.guardEnabled ~= false
    end
    return true -- fail closed: cannot read the pref => guard stays on
end

-- ---------------------------------------------------------------- primitives
local function isVC(photo)
    if photo == nil then return false end
    local ok, v = pcall(function() return photo:getRawMetadata('isVirtualCopy') end)
    return ok and v == true
end
Guard.isVirtualCopy = isVC

local function photoId(photo)
    local ok, id = pcall(function() return photo.localIdentifier end)
    if ok and id ~= nil then return tostring(id) end
    return "?"
end

local function denialMessage(ids)
    return "GUARD_DENIED: Develop writes are allowed only on virtual copies; not a virtual copy: photo id(s) "
        .. table.concat(ids, ", ") .. ". Hint: " .. Guard.HINT
end

-- Raises a string error "GUARD_DENIED: ..." unless `photo` is a virtual copy
-- (or the guard is disabled). Must be called immediately before the write.
function Guard.assertVirtualCopy(photo)
    if not Guard.isEnabled() then return true end
    if photo == nil then
        error("GUARD_DENIED: no target photo to check (refusing Develop write). Hint: " .. Guard.HINT, 0)
    end
    if not isVC(photo) then error(denialMessage({ photoId(photo) }), 0) end
    return true
end

local function activeCatalog() return import('LrApplication').activeCatalog() end

local function targetPhoto() return activeCatalog():getTargetPhoto() end

-- Error reply table for callbacks.
function Guard.denial(ids)
    return {
        success = false,
        error = {
            code = Guard.CODE,
            message = denialMessage(ids),
            severity = "error",
            details = { photoIds = ids, hint = Guard.HINT },
        },
    }
end

-- Pre-check for a command. Returns nil if allowed, else the offending ids.
-- Checks: the photos named by params.photoId / params.photoIds, and, for
-- non-batch calls, the current target photo (see below).
local function offenders(params)
    local catalog = activeCatalog()
    local bad, seen = {}, {}
    local function check(photo, label)
        if not isVC(photo) then
            local id = label or photoId(photo)
            if not seen[id] then seen[id] = true; table.insert(bad, id) end
        end
    end
    local ids = {}
    if params then
        if params.photoId ~= nil then table.insert(ids, params.photoId) end
        if type(params.photoIds) == "table" then
            for _, id in ipairs(params.photoIds) do table.insert(ids, id) end
        end
    end
    for _, id in ipairs(ids) do
        local photo = catalog:getPhotoByLocalId(tonumber(id))
        -- unknown id: the handler reports PHOTO_NOT_FOUND itself and writes nothing
        if photo then check(photo) end
    end
    -- Batch calls (non-empty photoIds) select each named photo themselves and the
    -- controller proxy re-checks at every write, so an unrelated original that
    -- happens to be selected is not an offender. Single-photo calls write to the
    -- TARGET photo (applySettings ignores photoId for the write), so check it.
    local isBatch = type(params) == "table" and type(params.photoIds) == "table" and #params.photoIds > 0
    if not isBatch then
        local target = catalog:getTargetPhoto()
        if target then check(target) elseif #ids == 0 then table.insert(bad, "(no target photo)") end
    end
    if #bad == 0 then return nil end
    return bad
end

local wrapped = setmetatable({}, { __mode = "k" })

function Guard.isWrapped(fn) return wrapped[fn] end

-- Wrap a handler(params, callback). The check and the handler call are
-- adjacent with no yield; the handler's own writes are additionally protected
-- by the guarded controller / assertVirtualCopy.
function Guard.wrap(name, handler)
    local fn = function(params, callback)
        if Guard.isEnabled() then
            local ok, bad = LrTasks.pcall(offenders, params)
            if not ok then
                bad = { "(guard check failed: " .. tostring(bad) .. ")" }
            end
            if bad then
                callback(Guard.denial(bad))
                return
            end
        end
        return handler(params, callback)
    end
    wrapped[fn] = name
    return fn
end

function Guard.wrapIfDW(name, handler)
    if dwSet[name] then return Guard.wrap(name, handler) end
    return handler
end

-- Drop-in for the router used in registerApiCommands: same register() call,
-- DW commands wrapped.
function Guard.routerProxy(router)
    return {
        register = function(_, name, handler, mode)
            return router:register(name, Guard.wrapIfDW(name, handler), mode)
        end,
    }
end

-- ---------------------------------------------------------- controller proxy
-- Read / UI functions that stay unguarded. Every OTHER function on
-- LrDevelopController is treated as a write (default-deny for new SDK calls).
local READ_PATTERNS = { "^get", "^select", "^goTo", "^toggle", "^reveal",
    "^addAdjustmentChangeObserver$", "^removeAdjustmentChangeObserver$" }

local function isReadName(name)
    for _, p in ipairs(READ_PATTERNS) do
        if string.find(name, p) then return true end
    end
    return false
end

-- Returns a plain table (so pairs() works) copying `ctl`; write functions
-- re-check the current target photo right before the real call. Keys that
-- pairs() did not enumerate (if the SDK namespace is not a plain table) are
-- resolved lazily through __index with the same rule.
function Guard.guardController(ctl)
    local function convert(k, v)
        if type(v) == "function" and type(k) == "string" and not isReadName(k) then
            return function(...)
                if Guard.isEnabled() then Guard.assertVirtualCopy(targetPhoto()) end
                return v(...)
            end
        end
        return v
    end
    local proxy = {}
    for k, v in pairs(ctl) do proxy[k] = convert(k, v) end
    return setmetatable(proxy, { __index = function(t, k)
        local v = ctl[k]
        if v == nil then return nil end
        local w = convert(k, v)
        rawset(t, k, w)
        return w
    end })
end

return Guard
