"""Drift test: every class-DW command registered in PluginInit.lua must be guarded."""
import re

from .conftest import PLUGIN_DIR

# Class DW (Develop write -> virtual copies only). PluginInit.lua's router:register list is
# the source of truth; a command that is in neither set fails the test, so new commands must
# be classified deliberately (and DW ones added to Guard.DW_COMMANDS).
DW = {
    "develop.applySettings", "develop.batchApplySettings", "develop.batchSetValue",
    "develop.setValue", "develop.resetToDefault", "develop.setAutoTone",
    "develop.setAutoWhiteBalance", "develop.setProcessVersion",
    "develop.resetAllDevelopAdjustments", "develop.setCurvePoints", "develop.setCurveLinear",
    "develop.setCurveSCurve", "develop.addCurvePoint", "develop.removeCurvePoint",
    "develop.createGreenSwatch", "develop.createCyanSwatch", "develop.enhanceColors",
    "develop.createNewMask", "develop.deleteMask", "develop.deleteMaskTool",
    "develop.addToCurrentMask", "develop.intersectWithCurrentMask",
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

# Class DR (develop reset): the single named exemption, allowed on originals, NOT wrapped.
DR = {"develop.clearAutoTone"}

NON_DW = {
    "system.ping", "system.status", "develop.getSettings", "develop.getValue",
    "develop.getRange", "develop.getProcessVersion", "develop.getCurvePoints",
    "develop.goToMasking", "develop.toggleOverlay", "develop.selectTool",
    "develop.getAllMasks", "develop.getSelectedMask", "develop.selectMask",
    "develop.getSelectedMaskTool", "develop.selectMaskTool", "develop.activateMaskingMode",
    "develop.getLocalValue", "develop.getAvailableLocalParameters",
    "develop.dumpLrDevelopController", "develop.discoverGradientParameters",
    "develop.monitorParameterChanges", "develop.probeAllDevelopParameters",
    "develop.editInPhotoshop", "catalog.searchPhotos", "catalog.getPhotoMetadata",
    "catalog.getSelectedPhotos", "catalog.setSelectedPhotos", "catalog.getAllPhotos",
    "catalog.findPhotoByPath", "catalog.findPhotos", "catalog.getCollections",
    "catalog.getKeywords", "catalog.getFolders", "catalog.batchGetFormattedMetadata",
    "catalog.setRating", "catalog.addKeywords", "catalog.setFlag", "catalog.getFlag",
    "catalog.setTitle", "catalog.setCaption", "catalog.setColorLabel", "catalog.copySettings",
    "catalog.createVirtualCopy", "catalog.setMetadata", "catalog.createCollection",
    "catalog.createSmartCollection", "catalog.createCollectionSet", "catalog.createKeyword",
    "catalog.removeKeyword", "catalog.setViewFilter", "catalog.getCurrentViewFilter",
    "catalog.removeFromCatalog", "catalog.deleteCollection", "catalog.countPhotos", "catalog.getCollectionPhotos", "catalog.getDevelopPresets",
    "preview.generatePreview", "preview.generateBatchPreviews", "preview.getPreviewInfo",
    "preview.getPreviewChunk", "selection.flagAsPick", "selection.flagAsReject",
    "selection.removeFlag", "selection.getFlag", "selection.setRating", "selection.getRating",
    "selection.setColorLabel", "selection.getColorLabel", "selection.nextPhoto",
    "selection.previousPhoto", "selection.selectAll", "selection.selectNone",
    "selection.selectInverse", "selection.increaseRating", "selection.decreaseRating",
    "selection.toggleRedLabel", "selection.toggleYellowLabel", "selection.toggleGreenLabel",
    "selection.toggleBlueLabel", "selection.togglePurpleLabel", "selection.extendSelection",
    "selection.deselectActive", "selection.deselectOthers",
}


def registered_names():
    text = (PLUGIN_DIR / "PluginInit.lua").read_text()
    return re.findall(r'router:register\("([\w.]+)"', text)


def test_classification_is_complete_and_disjoint():
    names = registered_names()
    assert len(names) == len(set(names)) and len(names) >= 130
    assert not (DW & NON_DW)
    assert not (DR & DW) and not (DR & NON_DW)
    unclassified = set(names) - DW - NON_DW - DR
    assert not unclassified, f"classify new command(s) as DW or NON_DW (and add DW ones to Guard.DW_COMMANDS): {sorted(unclassified)}"
    assert (DW | NON_DW | DR) - set(names) == set(), "classified but no longer registered"


def test_guard_dw_list_matches(env):
    lst = env.run("local G = require 'Guard'; local t = {}; for _, n in ipairs(G.DW_COMMANDS) do t[#t+1] = n end; return table.concat(t, ',')")
    assert set(lst.split(",")) == DW


REGISTER = """
package.loaded['SimpleSocketBridge'] = { start = function() end, stop = function() end,
    isRunning = function() return false end, isStarting = function() return false end }
T.addPhoto(1, false); T.select(T.photos[1])   -- an ORIGINAL is selected
dofile('%s/PluginInit.lua')
local B = _G.LightroomPythonBridge
local reg = {}
B.commandRouter = { register = function(self, name, h, mode) reg[name] = h; return true end }
B.registerSystemCommands(); B.registerApiCommands()
local Guard = require 'Guard'
"""


def test_every_dw_command_is_wrapped_and_denies_original(env):
    r = env.run(REGISTER % PLUGIN_DIR + """
        local wrapped, plain, denied = {}, {}, 0
        for name, h in pairs(reg) do
            if Guard.isWrapped(h) then
                wrapped[#wrapped + 1] = name
                local resp
                h({ photoId = 1, photoIds = { 1 } }, function(x) resp = x end)
                if resp and resp.error and resp.error.code == 'GUARD_DENIED' then denied = denied + 1 end
            else
                plain[#plain + 1] = name
            end
        end
        return table.concat(wrapped, ','), table.concat(plain, ','), denied, #T.writes
    """)
    wrapped, plain, denied, nwrites = r
    assert set(wrapped.split(",")) == DW
    assert set(plain.split(",")) == NON_DW | DR
    assert denied == len(DW)
    assert nwrites == 0


def test_dr_list_is_exactly_clear_auto_tone_and_disjoint(env):
    r = env.run("local G = require 'Guard'; return table.concat(G.DR_COMMANDS, ',')")
    assert set(r.split(",")) == DR


def test_dr_command_is_registered_unwrapped_and_not_denied_on_original(env):
    r = env.run(REGISTER % PLUGIN_DIR + """
        local h = reg['develop.clearAutoTone']
        local resp
        h({ photoIds = { 1 }, dryRun = true }, function(x) resp = x end)
        return Guard.isWrapped(h) and true or false, resp.error and resp.error.code or 'none'
    """)
    assert tuple(r) == (False, "none")
