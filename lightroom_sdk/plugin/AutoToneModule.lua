-- AutoToneModule.lua
-- develop.clearAutoTone: removes the settings Lightroom's "Auto Tone" wrote, on ANY
-- photo (originals included). This is the ONE deliberate, named exemption to the
-- virtual-copy rule (see Guard.DR_COMMANDS and CLAUDE.md). The exemption is
-- structural, not a flag:
--   * only the fixed key lists below are ever written (nothing the caller passes
--     is written), and they are only ever set to their default (0 / false);
--   * the write is photo:applyDevelopSettings, never LrDevelopController;
--   * one WriteGate.run per call; a non-executed gate replies WRITE_ABORTED.
-- optFlattenAutoNow is deliberately NOT passed (it would re-resolve Auto values).

local LrTasks = import 'LrTasks'
local WriteGate = require 'WriteGate'

local AutoToneModule = {}

AutoToneModule.MAX_IDS = 50
AutoToneModule.HISTORY_NAME = "Clear Auto Tone"

-- Tone sliders Auto Tone sets (evidence: 291 resolved + 684 placeholder merged DNGs
-- vs 5032 untouched ARWs; default 0 in all of them). A number ~= 0 is reset; that
-- covers resolved values and the -999999 "Auto pending" placeholder.
AutoToneModule.SLIDER_KEYS = {
    "Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012",
    "Whites2012", "Blacks2012", "Vibrance", "Saturation",
}
-- Auto-state flags; set to false only when getDevelopSettings reports true.
-- Applied in a separate pcall'd call because AutoTone is not documented in the SDK.
AutoToneModule.MARKER_KEYS = {
    "AutoTone", "AutoExposure", "AutoShadows", "AutoBrightness", "AutoContrast",
}

local function fail(code, message, details)
    return { success = false, error = { code = code, message = message, severity = "error", details = details } }
end

local function ok(result) return { success = true, result = result } end

local function pid(photo) return photo.localIdentifier end

-- Pure: settings table -> sliderReset, markerReset, changed, current (no side effects).
local function plan(settings)
    local sliderReset, markerReset, changed, current = {}, {}, {}, {}
    for _, k in ipairs(AutoToneModule.SLIDER_KEYS) do
        local v = settings[k]
        current[k] = v
        if type(v) == "number" and v ~= 0 then
            sliderReset[k] = 0
            changed[k] = { before = v, after = 0 }
        end
    end
    for _, k in ipairs(AutoToneModule.MARKER_KEYS) do
        local v = settings[k]
        current[k] = v
        if v == true then
            markerReset[k] = false
            changed[k] = { before = true, after = false }
        end
    end
    return sliderReset, markerReset, changed, current
end

local function isImage(photo)
    local good, fmt = pcall(function() return photo:getRawMetadata('fileFormat') end)
    -- fail closed: unknown format is refused too
    return good and fmt ~= nil and fmt ~= 'VIDEO'
end

function AutoToneModule.clearAutoTone(params, callback)
    params = params or {}
    local ids = params.photoIds
    if type(ids) ~= "table" or #ids == 0 then
        return callback(fail("INVALID_PARAM_VALUE", "photoIds must be a non-empty array of photo ids"))
    end
    if #ids > AutoToneModule.MAX_IDS then
        return callback(fail("INVALID_PARAM_VALUE",
            "at most " .. AutoToneModule.MAX_IDS .. " photoIds per call (got " .. #ids .. ")"))
    end
    local dryRun = params.dryRun == true

    local catalog = import('LrApplication').activeCatalog()

    -- Validate everything first: nothing is written if any id is bad.
    local photos, seen = {}, {}
    for _, raw in ipairs(ids) do
        local n = tonumber(raw)
        if not n or n <= 0 or n ~= math.floor(n) then
            return callback(fail("INVALID_PARAM_VALUE", "invalid photo id: " .. tostring(raw)))
        end
        if not seen[n] then
            seen[n] = true
            local photo = catalog:getPhotoByLocalId(n)
            if not photo then
                return callback(fail("PHOTO_NOT_FOUND", "Photo with ID " .. tostring(raw) .. " not found"))
            end
            if not isImage(photo) then
                return callback(fail("NOT_AN_IMAGE", "Photo " .. tostring(raw) .. " is not an image (video or unknown format)"))
            end
            table.insert(photos, photo)
        end
    end

    local results = {}

    local function process(photo, write)
        local entry = { id = pid(photo), changed = {} }
        local gotName, name = pcall(function() return photo:getFormattedMetadata('fileName') end)
        entry.filename = gotName and name or nil
        local gotSettings, settings = pcall(function() return photo:getDevelopSettings() end)
        if not gotSettings or type(settings) ~= "table" then
            entry.skippedReason = "could not read develop settings: " .. tostring(settings)
            return entry
        end
        local sliderReset, markerReset, changed, current = plan(settings)
        entry.current = current
        if next(changed) == nil then
            entry.skippedReason = "nothing to change"
            return entry
        end
        if not write then
            entry.changed = changed
            return entry
        end
        -- Sliders first, markers second, so an undocumented-key failure cannot block the sliders.
        entry.changed = {}
        if next(sliderReset) ~= nil then
            local good, err = pcall(function()
                photo:applyDevelopSettings(sliderReset, AutoToneModule.HISTORY_NAME)
            end)
            if good then
                for k in pairs(sliderReset) do entry.changed[k] = changed[k] end
            else
                entry.skippedReason = "applyDevelopSettings failed: " .. tostring(err)
                return entry
            end
        end
        if next(markerReset) ~= nil then
            local good, err = pcall(function()
                photo:applyDevelopSettings(markerReset, AutoToneModule.HISTORY_NAME)
            end)
            if good then
                for k in pairs(markerReset) do entry.changed[k] = changed[k] end
            else
                entry.markerError = tostring(err)
            end
        end
        return entry
    end

    if dryRun then
        for _, photo in ipairs(photos) do table.insert(results, process(photo, false)) end
    else
        local good, err = LrTasks.pcall(function()
            WriteGate.run(catalog, "Clear Auto Tone", function()
                for i, photo in ipairs(photos) do
                    table.insert(results, process(photo, true))
                    if i < #photos then LrTasks.yield() end
                end
            end, { timeout = 30 })
        end)
        if not good then
            return callback(fail(WriteGate.codeFor(err, "OPERATION_FAILED"), "Clear Auto Tone failed: " .. tostring(err)))
        end
    end

    return callback(ok({ dryRun = dryRun, count = #results, photos = results }))
end

return AutoToneModule
