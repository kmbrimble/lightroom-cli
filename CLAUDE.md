# CLAUDE.md

Fork of znznzna/lightroom-cli (MIT) used to let Claude control Adobe Lightroom Classic 15.6 on Kieren's Windows desktop.

## 1. What this project is

A Lua plugin (`lightroom_sdk/plugin/`, loaded by Lightroom as `lightroom-cli-bridge.lrplugin`) opens two local TCP sockets on 127.0.0.1 (54322 is the plugin's send port, 54323 its receive port, per `Config.lua`; the chosen ports are written to a port file, `$LR_PORT_FILE` or `<tempdir>/lightroom_ports.txt`) and speaks newline-delimited JSON (NDJSON) over them. 130 commands are registered in `lightroom_sdk/plugin/PluginInit.lua` and implemented in the `*Module.lua` files (Catalog, Develop, ...). The Python side is the SDK client (`lightroom_sdk/`), the `lr` Click CLI (`cli/`) and a FastMCP server (`mcp_server/`). All three talk to the plugin through the SDK client.

## 2. Test command

```
python -m pytest            # from repo root, inside a venv with `pip install -e '.[dev]'`
```

Runs `tests/` (pyproject sets `-v --tb=short`, asyncio auto mode): unit and CLI/MCP tests with mocked bridges. Baseline on Linux (2026-10-31, Python 3.11): 817 passed, 4 failed, 170 skipped in ~15 s. The 4 failures are in `tests/test_paths.py` (Windows/macOS path tests patch `lightroom_sdk.paths.sys`, which does not exist) and fail on unmodified upstream v1.2.2.

`tests/lua/` runs the real plugin Lua under Lua 5.1 (`pip install -e '.[dev,lua-tests]'`, needs `lupa`; skipped if absent) against stubbed Lightroom APIs: guard, write-gate aborts, autostart, registration drift. Still no substitute for a live check.

Needs live Lightroom: `tests/e2e/` (auto-skipped when the port file is missing or the ports refuse a TCP connection; `destructive` ones also need `--run-destructive`). **Never run e2e or any live test unless the orchestrator says so**; the plugin serves one client at a time.

Side effect: `tests/test_version_sync.py` runs `scripts/sync_version.py`, which rewrites tracked files (`Info.lua`, `PluginInit.lua`, two plugin JSONs). Run `git status` after pytest and `git checkout --` those files unless you are bumping a version.

## 3. Deploy and verify (manual, no CI deploy)

- Plugin files: copy `lightroom_sdk/plugin/*` to `%APPDATA%\Adobe\Lightroom\Modules\lightroom-cli-bridge.lrplugin` on the desktop, then in Lightroom: File > Plug-in Manager > Reload Plug-in. Lua changes take effect only after the reload.
- Python package: installed in `%LOCALAPPDATA%\lightroom-cli\venv` via `uv`. Reinstall there after Python changes.
- Verify: a live `lr system ping` from the orchestrator only (see constraints). Unit tests alone do not exercise the Lua.
- Nothing deploys automatically; pushing to GitHub changes nothing on the desktop.

## 4. Non-negotiable constraints

- **Never delete photo files from disk, and never expose a command that can.** Files: `lightroom_sdk/plugin/CatalogModule.lua` and the command registrations in `PluginInit.lua`. Why: the originals are irreplaceable; catalog-only removal is the most any command may do.
- **No Develop-module writes on original (non-virtual-copy) photos.** Files: `lightroom_sdk/plugin/Guard.lua`, `DevelopModule.lua`, `CatalogModule.lua`, `PluginInit.lua`. Why: edits to originals are the user's real work. Enforced in the plugin by `Guard.lua`: (1) `Guard.DW_COMMANDS` (49 class-DW commands) are wrapped at registration in `registerApiCommands` and reply `GUARD_DENIED` when the target photo or any named `photoId`/`photoIds` is not a virtual copy; (2) `DevelopModule` gets `LrDevelopController` via `Guard.guardController`, which re-checks the target photo immediately before every write function (default-deny: only get*/select*/goTo*/toggle*/reveal*/observer calls are unguarded); (3) the catalog-side Develop writes (preset, snapshot, paste, rotate) call `Guard.assertVirtualCopy(photo)` first inside the write gate. Pref `guardEnabled` (LrPrefs, default true) is read only by Guard.lua; never add a command that sets it. Policy stays light: library metadata, keywords, collections, flags, ratings and creating virtual copies are NOT restricted. New Develop-writing commands must be added to `Guard.DW_COMMANDS` (tests/lua fails on unclassified commands).
- **Catalog writes go through `WriteGate.run`** (`WriteGate.lua`): `withWriteAccessDo` with a timeout returns `"aborted"` without running the function, which used to mean no reply at all. Non-`executed` now raises/returns `WRITE_ABORTED`.
- **Auto-start:** `PluginInit.lua` starts the bridge itself ~2 s after plugin init when `autoStart` (default true; `Config:get` now honours a stored `false`) via `SimpleSocketBridge.start`, retrying 5x at 2 s. `start` is idempotent (`socketStarting` flag) so the menu item and autostart cannot double-bind. Reload re-runs PluginInit, which signals the previous instance's loop to close its sockets.
- **The plugin serves one client at a time; never run overlapping clients.** Files: `lightroom_sdk/plugin/SimpleSocketBridge.lua`, `lightroom_sdk/socket_bridge.py`. Why: a second client wedges the plugin until Lightroom reloads it. Only the orchestrator talks to the live bridge.
- **Every Lua `LrProgressScope` must be closed (`done()`/`cancel()`) on every path, including errors.** Files: `lightroom_sdk/plugin/PreviewModule.lua` (and any module using progress scopes). Why: a leaked scope leaves a stuck progress bar and can block Lightroom.
- **Escape backslashes in JSON strings.** File: `lightroom_sdk/plugin/MessageProtocol.lua` (`_encodeJSON`). Why: unescaped Windows paths made replies invalid JSON, the client dropped them, and the call ended in a 30 s timeout. Escape `\` first, then `"` and control chars.
- **Live Develop values are only readable while the Develop module is active.** File: `lightroom_sdk/plugin/DevelopModule.lua`. Why: `LrDevelopController` returns nothing useful in other modules; switch module first, do not treat nil as zero.
- **Keep CRLF line endings in Lua files.** Files: `lightroom_sdk/plugin/*.lua`. Why: the files are CRLF in the repo; an editor that normalises to LF produces whole-file diffs and breaks patch application and hash checks. Check with `git diff --stat` before committing.
- **Never print or commit secrets** (tokens, `.env`, credentials). Why: the repo is public. Credentials live in unraid-secretsman, reached via the `hl` CLI.

## Known gotchas

- Copy files to/from the desktop with `scp -O`; default scp silently truncates files at 200 KiB from this host.
- Multi-line PowerShell over ssh stdin fails silently: `scp -O` the script to `C:/NA2026/cl_scratch/` and run `powershell -NoProfile -ExecutionPolicy Bypass -File <script>`. Only write under `C:\NA2026\cl_scratch` on the desktop.
- Windows sshd does not kill child processes when the ssh session ends; never leave background processes running on the desktop.
- Never touch photos, the Lightroom catalog or its previews.
