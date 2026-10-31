"""Auto-start deferred task at the end of PluginInit.lua, and bridge start dedupe."""
from .conftest import PLUGIN_DIR

FAKE = """
F = { startCalls = 0, running = false, starting = false, upAfter = 1, raise = false }
package.loaded['SimpleSocketBridge'] = {
  start = function()
    F.startCalls = F.startCalls + 1
    if F.raise then error('boom') end
    if F.startCalls >= F.upAfter then F.running = true end
  end,
  stop = function() F.running = false end,
  isRunning = function() return F.running end,
  isStarting = function() return F.starting end,
}
"""


def init(env, setup=""):
    return env.run(FAKE + setup + f"""
        dofile('{PLUGIN_DIR}/PluginInit.lua')
        local s = {{}}
        for i, v in ipairs(T.sleeps) do s[i] = v end
        return F.startCalls, table.concat(s, ',')
    """)


def test_starts_bridge_once_after_delay(env):
    calls, sleeps = init(env)
    assert calls == 1
    assert sleeps.startswith("2,2")
    assert env.T.logHas("Auto-start: bridge is up")


def test_retries_until_up(env):
    calls, _ = init(env, "F.upAfter = 3")
    assert calls == 3
    assert env.T.logHas("Auto-start: bridge is up")


def test_gives_up_after_five_attempts_without_raising(env):
    calls, _ = init(env, "F.upAfter = 99")
    assert calls == 5
    assert env.T.logHas("gave up after 5 attempts")


def test_start_raising_is_contained_and_retried(env):
    calls, _ = init(env, "F.raise = true")
    assert calls == 5
    assert env.T.logHas("Auto-start: start raised")


def test_already_running_not_started_again(env):
    calls, _ = init(env, "F.running = true")
    assert calls == 0
    assert env.T.logHas("already running")


def test_start_in_progress_not_doubled(env):
    calls, _ = init(env, "F.starting = true")
    assert calls == 0


def test_autostart_pref_false_disables(env):
    calls, _ = init(env, "T.prefs.autoStart = false")
    assert calls == 0
    assert env.T.logHas("Auto-start disabled")


def test_reload_signals_previous_instance(env):
    r = env.run(FAKE + f"""
        _G.LightroomPythonBridge = {{ socketServerRunning = true }}
        local old = _G.LightroomPythonBridge
        dofile('{PLUGIN_DIR}/PluginInit.lua')
        return old.socketServerRunning, _G.LightroomPythonBridge ~= old
    """)
    assert tuple(r) == (False, True)


# ---- real SimpleSocketBridge: start dedupe / failure recovery
BRIDGE = f"""
T.deferTasks = true
_G.LightroomPythonBridge = {{ phase3Loaded = false, logger = nil }}
local B = require 'SimpleSocketBridge'
"""


def test_double_start_binds_once_and_menu_sees_starting(env, monkeypatch):
    r = env.run(BRIDGE + """
        B.start(); B.start()
        local queued, starting_before, running_before = #T.queue, B.isStarting(), B.isRunning()
        -- let the keep-alive loop exit after a few polls (as a reload/stop would)
        T.sleepHook = function(n) if n >= 3 then _G.LightroomPythonBridge.socketServerRunning = false end end
        T.runQueue()
        return queued, starting_before, running_before, T.binds, B.isStarting()
    """)
    assert tuple(r) == (1, True, False, 2, False)


def test_failed_bind_does_not_wedge_retries(env):
    r = env.run(BRIDGE + """
        T.bindFails = true
        B.start(); T.runQueue()
        local after_fail = { B.isStarting(), B.isRunning() }
        T.bindFails = false
        B.start()
        return after_fail[1], after_fail[2], #T.queue
    """)
    assert tuple(r) == (False, False, 1)
    assert env.T.logHas("Socket server failed")


def test_start_after_stop_clears_shutting_down(env):
    r = env.run(BRIDGE + """
        B.stop()
        local was = _G.LightroomPythonBridge.shuttingDown
        B.start()
        return was, _G.LightroomPythonBridge.shuttingDown
    """)
    assert tuple(r) == (True, False)


def test_menu_action_idempotent(env):
    r = env.run(BRIDGE + f"""
        _G.LightroomPythonBridge.initialized = true
        B.start()
        local before = #T.queue
        dofile('{PLUGIN_DIR}/MenuActions.lua')   -- start in flight: must not start again
        return before, #T.queue, #T.messages, string.find(T.messages[1], 'already running', 1, true) ~= nil
    """)
    assert tuple(r) == (1, 1, 1, True)
