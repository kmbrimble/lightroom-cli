"""Offline Lua 5.1 harness (lupa): stubs Lightroom's `import` and runs the real plugin
modules from lightroom_sdk/plugin. Skips cleanly when lupa is not installed
(`pip install -e '.[lua-tests]'`)."""
import os
from pathlib import Path

import pytest

lupa_lua51 = pytest.importorskip("lupa.lua51")

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "lightroom_sdk" / "plugin"

PRELUDE = r"""
T = { writes = {}, log = {}, sleeps = {}, queue = {}, deferTasks = false, gate = "executed",
      photos = {}, target = nil, selection = {}, sleepHook = nil, bindFails = false, binds = 0,
      prefs = {}, messages = {} }

function T.addPhoto(id, isVC)
  local p = { localIdentifier = id, vc = isVC, ops = {} }
  function p:getRawMetadata(k)
    if k == 'isVirtualCopy' then return self.vc end
    return nil
  end
  function p:getFormattedMetadata(k) return tostring(id) end
  for _, m in ipairs({'rotateLeft','rotateRight','pasteSettings','createDevelopSnapshot','applyDevelopPreset','setRawMetadata'}) do
    p[m] = function(self, ...) table.insert(T.writes, { op = m, photo = id }) end
  end
  T.photos[id] = p
  return p
end

function T.select(photo) T.target = photo; T.selection = { photo } end

local catalog = {}
function catalog:getPhotoByLocalId(id) return T.photos[id] end
function catalog:getTargetPhoto() return T.target end
function catalog:getTargetPhotos() return T.selection end
function catalog:setSelectedPhotos(p, list) T.target = p; T.selection = list or { p } end
function catalog:withWriteAccessDo(name, fn, opts)
  T.lastGate = name
  if T.gate == "executed" then T.inGate = true; local ok, e = pcall(fn); T.inGate = false; if not ok then error(e, 0) end end
  return T.gate
end
-- Real SDK: LrCatalog:createVirtualCopies(copyName) acts on the SELECTED photos, returns the new photos.
-- It is modelled as needing write access, the conservative reading of the undocumented requirement.
function catalog:createVirtualCopies(copyName)
  if not T.inGate then error("createVirtualCopies needs write access", 0) end
  local out = {}
  for _, src in ipairs(T.selection) do
    local id = 1000 + #T.writes + 1
    local vc = T.addPhoto(id, true)
    table.insert(T.writes, { op = 'createVirtualCopies', photo = src.localIdentifier })
    out[#out + 1] = vc
  end
  return out
end
function catalog:createCollection(name, parent, returnExisting)
  T.collections = T.collections or {}
  local c = T.collections[name]
  if not c then
    c = { localIdentifier = 500 + #T.writes, name = name, photos = {} }
    function c:addPhotos(list) for _, p in ipairs(list) do table.insert(self.photos, p) end end
    function c:getPhotos() return self.photos end
    function c:getName() return self.name end
    function c:delete() T.collections[self.name] = nil end
    T.collections[name] = c
  end
  return c
end
function catalog:getCollectionByLocalIdentifier(id)
  for _, c in pairs(T.collections or {}) do if c.localIdentifier == id then return c end end
end
function catalog:setActiveSources(list) T.activeSources = list end
function catalog:getAllPhotos() local o = {}; for _, p in pairs(T.photos) do o[#o + 1] = p end; return o end
function catalog:createKeyword() end
T.catalog = catalog

local function noop() end
local autostub = setmetatable({}, { __index = function() return noop end })

local Tasks = {}
Tasks.pcall = pcall
function Tasks.sleep(s)
  table.insert(T.sleeps, s)
  if T.sleepHook then T.sleepHook(#T.sleeps, s) end
end
function Tasks.yield() end
function Tasks.startAsyncTask(f)
  if T.deferTasks then table.insert(T.queue, f) else f() end
end
function T.runQueue()
  while #T.queue > 0 do local f = table.remove(T.queue, 1); f() end
end

local ctl = { writes = T.writes }
-- reads
function ctl.getValue(n) return 0 end
function ctl.getRange(n) return -5, 5 end
function ctl.getProcessVersion() return "pv" end
function ctl.getSelectedTool() return "loupe" end
function ctl.selectTool() end
function ctl.goToMasking() end
function ctl.toggleOverlay() end
function ctl.addAdjustmentChangeObserver() end
-- writes
for _, n in ipairs({'setValue','resetToDefault','createNewMask','resetAllDevelopAdjustments','setAutoTone',
                    'addToCurrentMask','deleteMask','resetCrop','someFutureWrite'}) do
  ctl[n] = function(...)
    table.insert(T.writes, { op = n, args = { ... }, target = T.target and T.target.localIdentifier })
  end
end
ctl.CONSTANT = 42
T.ctl = ctl

local prefsT = T.prefs
local stubs = {
  LrTasks = Tasks,
  LrPrefs = { prefsForPlugin = function() return prefsT end },
  LrApplication = { activeCatalog = function() return catalog end, developPresetFolders = function() return { { getDevelopPresets = function() return { { getName = function() return 'p' end } } end } } end },
  LrDevelopController = ctl,
  LrApplicationView = { getCurrentModuleName = function() return "develop" end, switchToModule = noop },
  LrUndo = { performWithUndo = function(n, f) return f() end },
  LrDialogs = { message = function(t, m) table.insert(T.messages, m) end, showBezel = noop },
  LrFunctionContext = { callWithContext = function(n, f) return f({}) end },
  LrSocket = { bind = function(args)
      T.binds = T.binds + 1
      if T.bindFails then error("port in use") end
      return { close = noop, send = noop, reconnect = noop }
    end },
  LrPathUtils = { getStandardFilePath = function() return T.tmpdir end,
                  child = function(a, b) return a .. "/" .. b end },
  LrFileUtils = { exists = function() return false end, delete = noop },
  LrLogger = function(name)
    local o = {}
    for _, lvl in ipairs({'trace','debug','info','warn','error'}) do
      o[lvl] = function(self, msg) table.insert(T.log, lvl .. ": " .. tostring(msg)) end
    end
    o.enable = noop
    return o
  end,
}
function import(n) return stubs[n] or autostub end
_PLUGIN = {}

-- helper: did the log contain a substring
function T.logHas(sub)
  for _, l in ipairs(T.log) do if string.find(l, sub, 1, true) then return true end end
  return false
end
"""


class Env:
    def __init__(self, tmp_path):
        from lupa.lua51 import LuaRuntime

        self.lua = LuaRuntime(unpack_returned_tuples=True)
        g = self.lua.globals()
        g.package.path = str(PLUGIN_DIR) + "/?.lua"
        self.lua.execute(PRELUDE)
        self.T = g.T
        self.T.tmpdir = str(tmp_path)

    def run(self, code):
        return self.lua.execute(code)

    def eval(self, code):
        return self.lua.eval(code)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
