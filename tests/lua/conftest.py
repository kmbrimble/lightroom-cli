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

function T.addPhoto(id, isVC, dev, fmt)
  local p = { localIdentifier = id, vc = isVC, ops = {}, dev = dev or {}, fmt = fmt or 'DNG' }
  function p:getRawMetadata(k)
    if k == 'isVirtualCopy' then return self.vc end
    if k == 'fileFormat' then return self.fmt end
    return nil
  end
  function p:getFormattedMetadata(k) if k == 'fileName' then return 'photo' .. id .. '.dng' end return tostring(id) end
  function p:getDevelopSettings() local c = {}; for k, v in pairs(self.dev) do c[k] = v end; return c end
  -- applyDevelopSettings MERGES into the settings (omitted keys are untouched)
  function p:applyDevelopSettings(tbl, name, flatten)
    if T.applyFails then error("apply failed") end
    local rec = { op = 'applyDevelopSettings', photo = id, name = name, flatten = flatten, keys = {} }
    for k, v in pairs(tbl) do self.dev[k] = v; rec.keys[k] = v end
    table.insert(T.writes, rec)
  end
  for _, m in ipairs({'rotateLeft','rotateRight','pasteSettings','createDevelopSnapshot','applyDevelopPreset','setRawMetadata','createVirtualCopy'}) do
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
  if T.gate == "executed" then fn() end
  return T.gate
end
function catalog:createCollection() end
function catalog:createKeyword() end
function catalog:removePhoto() end
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
