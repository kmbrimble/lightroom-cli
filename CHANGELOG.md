# Changelog

All notable changes to this project will be documented in this file.

## Unreleased (kmbrimble fork)

### Added
- `Guard.lua`: Develop-module writes are refused (`GUARD_DENIED`) unless the photo is a virtual copy; command wrappers plus a guarded `LrDevelopController` re-check right before each write. Pref `guardEnabled` (default true).
- Plugin auto-start (`autoStart`): the socket bridge now starts itself after plugin init, with retries; start is idempotent and the menu item reports "already running".
- `WriteGate.lua`: catalog write handlers return `WRITE_ABORTED` instead of never replying when the write gate is not obtained.
- `tests/lua`: offline Lua 5.1 tests (optional `lua-tests` extra, needs `lupa`).
- Persistent per-desktop daemon `lrd` (`lightroom_sdk/daemon/`) that holds the single plugin connection, multiplexes many clients over a loopback HTTP API, and enforces the virtual-copy rule; stdlib-only thin client `lightroom_sdk/lrc.py`; Windows Scheduled Task installer under `packaging/windows/`; see `docs/daemon.md`
- `scripts/catalog_cleanup.py`: one-off resumable catalog clean-up runner (remove `-HDR-2.dng` duplicates, remove bracket source ARWs of merged sets, clear Auto Tone) with preflight assertions, NDJSON audit log and no disk-delete code path; see `docs/catalog-cleanup.md`

- `develop.clearAutoTone` (class DR, `AutoToneModule.lua`): removes Auto Tone (8 tone sliders -> 0, Auto* flags -> false) from any photo incl. originals, `{photoIds (max 50), dryRun}`; the single named exemption to the virtual-copy rule (`Guard.DR_COMMANDS`), daemon class `DR`. Key evidence: `/projects/.lr-scratch/autotone/keys.md`
- `lr_quality/` + `lr-quality` CLI: offline exposure / histogram / focus / tilt / noise metrics for a JPEG (extras `quality`, `quality-fast`), with batch-relative scoring, `needs_second_opinion` and `learn-targets`; separate package, no Lightroom dependency; see `docs/quality.md`

### Fixed
- Windows paths in plugin replies produced invalid JSON (unescaped backslashes), so the client dropped them and the call timed out after 30 s — the Lua JSON string encoder now escapes `\`, `"` and control characters
- `LrProgressScope` leak in the "Generating Preview" path left a stuck progress bar — the scope is now always closed, and thumbnail waits are bounded (commit bd94810)
- ~4 s of latency per call on Windows — clients connected to `localhost`, which resolves to `::1` first while the plugin listens only on IPv4 `127.0.0.1`, so each call paid ~2 s per refused `::1` attempt (two connects per call). The default host is now `127.0.0.1`; override with `LR_BRIDGE_HOST`
- `tests/test_paths.py` failures (module-level `import sys` in `paths.py` so tests can patch it)
- `Config:get` ignored a stored `false` (so `autoStart = false` could never take effect).
- `SimpleSocketBridge`: a failed bind no longer wedges later start attempts; starting after Stop clears `shuttingDown`.

### Changed
- `tests/test_version_sync.py` runs against a temporary copy and no longer rewrites tracked files
- `CLAUDE.md` is now tracked (removed from `.gitignore`)

## [1.2.2] - 2026-04-06

### Fixed
- macOS でポートファイルが見つからず接続できない問題を修正 — `/tmp` ハードコードを `tempfile.gettempdir()` に変更（macOS の実際のテンポラリディレクトリは `/var/folders/...` であり `/tmp` と異なる）

## [1.2.0] - 2026-03-07

Stability improvements, batch develop, extended search filters, and new catalog commands.

### Added
- **Extended search filters** for `catalog find` — `--folder-path`, `--capture-date-from`, `--capture-date-to`, `--file-format`, `--keyword`, `--filename`
- **`develop batch-set`** — batch set a single develop parameter across multiple photos (`--photo-ids`)
- **`catalog collection-photos`** — get photos from a collection by ID with pagination
- **`catalog develop-presets`** — list/search develop presets by name or folder
- **NDJSON streaming** — `StreamAggregator` for chunked responses with progress callbacks
- **Command cancellation** — Lua-side `shouldAbort()` checks at chunk boundaries
- **Version sync** — `scripts/sync_version.py` + CI `check-version` job
- **`protocolVersion`** field in ping response

### Fixed
- `searchPhotos` backward compatibility — delegates to `findPhotos` with `hasMore` field preserved
- `captureDateTo` inclusive date handling — date-only input appends `T23:59:59`
- Keyword filter uses plain text matching (`string.find` with `plain=true`)
- Stream cleanup — `_pending_streams` entry removed after final event
- `getCommandRouter` uses correct global key (`commandRouter`, not `router`)
- Negative offset crash in `getCollectionPhotos` — clamped with `math.max`

### Changed
- `searchPhotos` deprecated in favor of `findPhotos`
- 814+ tests (was 750+)

## [1.1.0] - 2026-03-06

MCP Server support, Windows compatibility, and reliability improvements.

### Added
- **MCP Server** — `lr-mcp` entry point for Claude Desktop / Cowork integration
  - `lr mcp install` / `uninstall` / `status` / `test` commands
  - All 107 CLI commands available as MCP tools (`lr_` prefix + snake_case)
  - Auto-resolves absolute path for Claude Desktop's PATH-limited environment
- **Windows support** — platform-aware path resolution (`platformdirs`), CI matrix with `windows-latest`. Not yet tested on real hardware — [please report issues](https://github.com/znznzna/lightroom-cli/issues)
- **Input validation layer** — `lightroom_sdk/validation.py` with type coercion, range checks, enum validation, string sanitization
- **Schema-driven architecture** — `lightroom_sdk/schema.py` as single source of truth for all command parameters
- **`--json` / `--json-stdin`** — JSON input for all commands, enables pipe-based workflows
- **`--dry-run`** — preview command execution without sending to Lightroom

### Fixed
- Schema/Lua parameter name mismatches (14 commands fixed)
- `catalog set-rating 0` — Lua `and/or` idiom fails with `nil`; use explicit `if` statement
- `catalog remove-keyword` — pass keyword object instead of string to `photo:removeKeyword()`
- `catalog select` — `withWriteAccessDo` return value not propagated; use external variable
- `catalog batch-metadata` — iterate photo-keyed table instead of integer-indexed
- `develop range` — `math.abs()` for correct min/max when range is negative number
- `develop set` — unknown parameter detection changed from `getRange` to `getValue`
- Exit code 0 on Lightroom errors — added `success: false` check in `helpers.py`

### Changed
- `fastmcp` is now a required dependency (was optional `[mcp]` extra)
- `develop set` with multiple pairs uses individual `setValue` calls instead of `batchApplySettings`
- 750+ tests (was 680+)

## [1.0.0] - 2026-03-06

Initial public release — 107 CLI commands covering all Lightroom Classic Lua API operations.

### Highlights
- **CLI tool (`lr`)** with `system`, `catalog`, `develop`, `preview`, `selection`, `plugin` command groups
- **107 commands** for full Lightroom Classic control
- **AI Mask API** — subject/sky/background/people/landscape with adjustments and presets
- **Agent-first design** — `lr schema` for dynamic discovery, `--fields`, `--dry-run`, structured JSON errors
- **ResilientSocketBridge** with auto-reconnect, heartbeat, per-command timeouts
- **Lua plugin** bundled and installable via `lr plugin install`
- **3 output formats**: `text`, `json`, `table`
- **680+ tests** (unit + integration)
