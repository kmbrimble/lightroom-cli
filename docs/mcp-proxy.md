# lr-mcp-proxy: MCP server for Lightroom via the lrd daemon

`lightroom_sdk/mcp_proxy.py` is a stdio MCP server that talks **only to the `lrd` daemon** (HTTP 127.0.0.1:54330), never to the
plugin bridge. The older `lr-mcp` server (`mcp_server/`) connects straight to the bridge: with the daemon running it would
become a second bridge client and wedge the plugin. Register `lr-mcp-proxy` instead, never both.

Every tool is one `POST /v1/call` (client label `mcp-proxy`), so the daemon's guard, scheduler and audit log apply. The proxy
adds no policy except: a fixed tool set, `lr_call` limited to read commands, preview size cap/rate limit, and basic argument
checks. It reads the daemon address and token exactly like `lrc.py` (reuses `lightroom_sdk.lrc`): `$LRD_HOME` or
`%LOCALAPPDATA%\lightroom-cli\lrd.json`, with the one-time token re-read and (Windows only) scheduled-task self-heal.
The daemon must be running on the machine where the proxy process runs.

## Tools

| Tool | Daemon command (class) | Notes |
|---|---|---|
| `lr_status` | `GET /v1/health` + `system.ping` (R) | bridge state, queue, uptime |
| `lr_count_photos` | `catalog.countPhotos` (R) | |
| `lr_list_photos` | `catalog.getAllPhotos` (R) | `limit` max 200, `offset` |
| `lr_get_selected` | `catalog.getSelectedPhotos` (R) | read only |
| `lr_photo_metadata` | `catalog.getPhotoMetadata` (R) | |
| `lr_render_preview` | `preview.generatePreview` (R) | returns an MCP image block; size capped at 1600; one per 2 s (`LR_PROXY_PREVIEW_MIN_INTERVAL`) |
| `lr_find_photos` | `catalog.findPhotos` (R) | `search_desc` dict, max 200 per page |
| `lr_get_collections` | `catalog.getCollections` (R) | |
| `lr_add_keywords` | `catalog.addKeywords` (LM) | no "set/replace keywords" command exists in the plugin, so `lr_set_keywords` is not offered |
| `lr_set_rating` / `lr_set_flag` | `catalog.setRating` / `catalog.setFlag` (LM) | |
| `lr_set_title_caption` | `catalog.setTitle`, `catalog.setCaption` (LM) | two sequential calls |
| `lr_set_gps_metadata` | `catalog.setMetadata` key `gps` (LM) | generic path; the plugin has no dedicated GPS command (see caveats) |
| `lr_create_virtual_copy` | `catalog.createVirtualCopy` (VC) | |
| `lr_apply_develop_settings` | `develop.applySettings` (DW) | daemon answers `GUARD_DENIED` on originals |
| `lr_clear_auto_tone` | `develop.clearAutoTone` (DR) | `dry_run` defaults to **true**; max 50 ids; allowed on originals (the one named exemption) |
| `lr_stage_removal` | `catalog.removeFromCatalog` (RM) | **stages only**: adds photos to the collection `_LRCLI_TO_REMOVE` and shows it; the real "Remove from Catalog" is a manual UI step. The proxy sets the daemon's `allow_remove_from_catalog` flag because this tool cannot do more than stage |
| `lr_call` | any command whose class is R or RD | refuses everything else (and unknown names) with a clear message; also refuses `preview.*` (use `lr_render_preview`) and the probe commands `develop.monitorParameterChanges`, `probeAllDevelopParameters`, `discoverGradientParameters`, `dumpLrDevelopController`, `catalog.copySettings` |

The class table comes from the running daemon (`GET /v1/commands`), not from a copy in the proxy. No tool takes a file path and
none can delete anything on disk. Errors from the daemon are raised as MCP tool errors carrying the daemon's JSON error
(`GUARD_DENIED`, `BRIDGE_DOWN`, `LR_TIMEOUT`, ...). The tool descriptions and server instructions tell the model: one operation
at a time, ids are Lightroom local ids, stacks are collapsed (only the top photo shows), do not rely on selection
(`setSelectedPhotos` is unreliable), Develop edits need a virtual copy.

## Install

On the desktop, into the existing venv (`fastmcp` is already a core dependency):

```
uv pip install --python %LOCALAPPDATA%\lightroom-cli\venv\Scripts\python.exe <path-to-this-branch-checkout>
```

Console script `lr-mcp-proxy` is also installed; use `python -m lightroom_sdk.mcp_proxy` in registrations (survives a missing
Scripts dir and lets you pass `-u`).

## Registration (do not run until the desktop venv has this branch installed and `lrc health` shows `bridge: connected`)

Let `PY` be the venv interpreter on the desktop, e.g. `C:/Users/<USER>/AppData/Local/lightroom-cli/venv/Scripts/python.exe`
(find it with `ssh 9950x "echo %LOCALAPPDATA%"` if the remote shell is cmd, or `ssh 9950x '$env:LOCALAPPDATA'` for PowerShell).

### (a) Claude Code in the container, over ssh stdio

```
claude mcp add lightroom -s user -- ssh -T -o BatchMode=yes -o ServerAliveInterval=30 -o LogLevel=ERROR 9950x C:/Users/<USER>/AppData/Local/lightroom-cli/venv/Scripts/python.exe -u -m lightroom_sdk.mcp_proxy
```

Why each piece (the stdio stream is the protocol, so anything extra on stdout corrupts it):

- `-T`: no pty. A pty would add `\r`, echo input and merge stderr into stdout. Claude Code gives ssh no tty anyway; `-T` makes it explicit.
- `BatchMode=yes`: never prompt for a password/host-key on the stdio pipe (the key is already set up in `~/.ssh/config` for `9950x`).
- `ServerAliveInterval=30`: detects a dead desktop instead of hanging the tool call.
- `LogLevel=ERROR`: suppresses ssh's own notices ("Warning: Permanently added ...") which go to stderr, harmless but noisy.
- Use a **literal path with forward slashes and no spaces**. Over Windows OpenSSH the command string is re-parsed by the remote default
  shell (cmd.exe or PowerShell): `%LOCALAPPDATA%` only expands in cmd, `$env:LOCALAPPDATA` only in PowerShell, and a path with
  spaces or backslashes needs shell-specific quoting. If PowerShell is the default shell, an unquoted `C:/...python.exe` is a bare
  word and fails with "unexpected token": quote it as `'& C:/.../python.exe -u -m lightroom_sdk.mcp_proxy'` (one argument).
- PowerShell **profiles** can print banners to stdout: if the remote default shell is PowerShell and a profile exists, use
  `powershell -NoProfile -NonInteractive -Command "& 'C:/.../python.exe' -u -m lightroom_sdk.mcp_proxy"` as the remote command.
- `python -u` plus the proxy's own `write_through` stdout: no buffering, so replies are not held back. The proxy prints no banner
  (`show_banner=False`) and logs to stderr only. On Windows the text layer writes `\r\n`; JSON-RPC readers accept the extra `\r`.
- Closing stdin (ssh dropping) ends the proxy. Windows sshd does not always reap children, but the proxy spawns none (the only
  subprocess is `schtasks /run` for daemon self-heal, which exits at once).
- Never start `lrd` from this ssh session; the proxy's self-heal uses the scheduled task only.

Check without registering: `ssh -T 9950x C:/.../python.exe -u -m lightroom_sdk.mcp_proxy < /dev/null` must print nothing on stdout
(it exits when stdin closes). **Do not run a real tool call while another agent owns the bridge**; `lr_status` is safe (health GET + ping through the daemon).

### (b) Local on the desktop (Claude Code running on the desktop)

```
claude mcp add lightroom -s user -- C:/Users/<USER>/AppData/Local/lightroom-cli/venv/Scripts/python.exe -u -m lightroom_sdk.mcp_proxy
```

No ssh layer; `LRD_HOME` (if non-default) can be added with `-e LRD_HOME=...` before the `--`.

## Tests

`python -m pytest tests/mcp_proxy` (~40 s; each test spawns the proxy as a real stdio subprocess against the in-process daemon and
`tests/daemon/mock_plugin.py`; nothing touches Lightroom): tool list, request shape of every tool, daemon guard denial on originals,
`lr_call` refusals, preview cap and rate limit, argument checks never reaching the daemon.

## Caveats

- `lr_set_gps_metadata` goes through `catalog.setMetadata` -> `photo:setRawMetadata("gps", {latitude,longitude})`. Whether the plugin's
  JSON decoder passes the nested table and Lightroom accepts it is **untested on real Lightroom**.
- `lr_apply_develop_settings` assumes `develop.applySettings` takes `{photoId, settings}` (read from the Lua, not exercised live).
- `lr_clear_auto_tone` is allowed on originals by design (documented exemption in CLAUDE.md); the model should keep `dry_run` true first.
- Previews over ~10 MB base64 are delivered by the plugin as `CHUNKED_TRANSFER`; the proxy reports an error rather than reassembling (not reachable at <=1600 px).
- Calls are not serialized in the proxy; the daemon's scheduler does that.
