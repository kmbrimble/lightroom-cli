# lrd: the persistent Lightroom bridge daemon

One long-lived process per desktop (`lrd`) owns the single connection pair to the Lightroom CLI Bridge plugin
(`127.0.0.1:54323` for writing, then `:54322` for reading, NDJSON) and serves any number of clients over a
localhost HTTP API. It removes the per-call connect cost, the "second client wedges the plugin" failure and the
plugin's restart-on-disconnect churn, and hosts the Guard. This page is the operator and client reference.

Stdlib only on the runtime path. No pydantic, click, rich or fastmcp is imported by `lightroom_sdk/daemon/*` or `lrc.py`
(note: `python -m lightroom_sdk.daemon` still executes the package `__init__`, which imports pydantic once at start-up,
about 0.2 s, harmless for a long-lived process).

## Files and state

State directory: `$LRD_HOME`, default `%LOCALAPPDATA%\lightroom-cli` on Windows (`~/.local/state/lightroom-cli` elsewhere).

| File | Purpose |
|---|---|
| `lrd.json` | `{"port","token","pid","started"}`, rewritten at every daemon start, user-only ACL (`0600` / `icacls`) |
| `lrd.lock` | singleton lock held while the daemon runs (a second instance exits 0 quietly) |
| `lrd.log` | daemon log (rotating, 2 MB x 3). The daemon never prints: `pythonw` has no stdio |
| `audit.jsonl` | one JSON line per non-read call, rotated at 5 MB to `audit.jsonl.1` |

Environment: `LRD_HOME`, `LRD_PORT` (default 54330), `LR_PORT_FILE` (plugin port file, same variable the SDK uses; default
`<tempdir>/lightroom_ports.txt`, falls back to 54322/54323 if absent or stale), `LRD_LOG_LEVEL`.

## Start and stop

Windows (per user, no admin): `packaging/windows/install-lrd.ps1` registers the Scheduled Task `LightroomCliDaemon`
(at logon, LeastPrivilege, restart on failure every minute), copies `lrc.py` and an `lrc.cmd` shim to the state dir and
starts it. `uninstall-lrd.ps1` stops and removes it. Never start the daemon directly from an ssh session (Windows sshd
does not reap children); always `schtasks /run /tn LightroomCliDaemon` (the thin client does this itself when the
daemon is unreachable, on Windows only).

Stop: `lrc stop` (graceful: drains in-flight calls up to 5 s, closes sockets, deletes `lrd.json`), fallback
`schtasks /end /tn LightroomCliDaemon`.

Manual run (development): `python -m lightroom_sdk.daemon` (entry point `lrd` is also installed).

## Thin client `lrc`

Run it as a **file** with isolation flags, never via `-m` or a console script (importing the `lightroom_sdk` package would
pull pydantic and cost ~170 ms; running the file from inside the package also needs the sys.path scrub that `lrc.py` does):

```
python -I -S lrc.py call catalog.getSelectedPhotos
python -I -S lrc.py call develop.setValue '{"parameter":"Exposure","value":0.5}' --timeout 20 --client claude
python -I -S lrc.py call catalog.removeFromCatalog '{"photoId":123}' --allow-remove-from-catalog
python -I -S lrc.py batch calls.json          # or "-" for stdin: [{"command":...,"params":{...}}, ...]
python -I -S lrc.py health | ping | commands | stop
```

Options: `--timeout S` (deadline; default 30, previews 110, `develop.batchAIMask` 280), `--client NAME` (audit label),
`--wait-bridge S` (how long to wait for a down bridge, default 2), `--request-id ID` (so `/v1/cancel` can address the call),
`--allow-remove-from-catalog`, `--allow-external-edit`.

Output: the daemon's JSON on stdout, verbatim. Exit codes: `0` ok, `1` Lightroom/other error, `2` guard denied,
`3` bridge or daemon down, `4` timeout. A wrong/rotated token is re-read once from `lrd.json`, then reported as
`UNAUTHORIZED` (exit 1).

## HTTP protocol (127.0.0.1:54330)

Every request needs: `Host: 127.0.0.1:<port>` (else 403), header `X-Lrd-Token: <token from lrd.json>` (else 401), and for POST
`Content-Type: application/json` (else 415). OPTIONS is 405 and no `Access-Control-*` header is ever sent, so browsers cannot
call it. One request per connection.

```
POST /v1/call     {"command","params","timeout","flags":{},"client","wait_bridge","request_id"}
  -> {"ok":true,"result":{...},"ms":{"queue":3,"lr":41}}
  -> {"ok":false,"error":{"code","message","details":{}}}        # HTTP 200 either way
POST /v1/batch    {"calls":[{...}],"ordered":true,"stop_on_error":true}   # <= 200 calls; -> {"ok","results":[...]}
POST /v1/cancel   {"requestId":"<request_id of a running call>"}           # forwards cancelCommand
POST /v1/shutdown
GET  /v1/health   {"bridge":"connected|connecting|down","bridge_reason","inflight","queued","uptime_s","connects","plugin",...}
GET  /v1/commands the class table (name -> class)
```

Error codes: `GUARD_DENIED`, `BRIDGE_DOWN` (nothing was sent), `BRIDGE_LOST` (link died mid-call; for writes
`details.outcome="unknown"`), `LR_TIMEOUT` (deadline hit, `cancelCommand` sent; `outcome` unknown for writes),
`BAD_REQUEST`, plus whatever code the plugin returns (`HANDLER_ERROR`, `UNKNOWN_COMMAND`, ...).
Streaming commands (`_stream`) and `cancelCommand` as a command are not exposed.

## Scheduling

Pure reads (class `R`, 23 commands) run in parallel, at most 4 at a time. Everything else (Develop reads, UI/selection,
metadata and Develop writes) runs one at a time in strict arrival order and also waits for in-flight reads, because the
plugin's Develop and selection state is global. On a deadline the call returns `LR_TIMEOUT`, `cancelCommand` is sent,
and the exclusive lane stays held until the plugin answers or 5 s pass, so a still-running write cannot overlap the next
command. Client disconnects do not abort a call that has already started.

## Guard (deliberately light)

Only routine blocking rule: **Develop-module writes (class DW, 49 commands) are allowed only on virtual copies.**
The daemon resolves the affected photos (`photoId`/`photoIds`, else the current selection via
`catalog.getSelectedPhotos`), requires `isVirtualCopy == true` for every one (missing or unknown counts as no, empty selection
is denied) and holds the exclusive lane across resolve, check and execute. Denial:
`GUARD_DENIED` with `details.offending` and a hint to use `catalog.createVirtualCopy`.

Everything else on originals passes with no flags: keywords, collections, flags, ratings, titles, captions, location fields,
`catalog.setMetadata` with any key, creating virtual copies.

Hard rules: unknown command names are denied (`classes.py` must equal the plugin's `router:register` list; a test parses
`PluginInit.lua`); names that could delete files from disk are denied by a regex no flag overrides;
`catalog.removeFromCatalog` needs `flags.allow_remove_from_catalog=true`; `develop.editInPhotoshop` needs
`flags.allow_external_edit=true`.

Known limits: if a DW handler ignores `photoId` and acts on the target photo, the daemon checks the id named, not the
target (the Lua backstop `Guard.lua` is what closes that); a human changing selection in the Lightroom UI between check and
write is only preventable inside the plugin. Selection-dependent DW calls are checked against the selection at check time.

## Failure modes

| Situation | Behaviour |
|---|---|
| Lightroom not running / plugin not started | `BRIDGE_DOWN` fast; background reconnect 0.5 s to 10 s backoff; `health.bridge_reason` explains |
| Lightroom restarts | in-flight calls get `BRIDGE_LOST`; reconnects when listeners reappear (needs plugin autoStart or the menu item) |
| Another client already holds the plugin | the handshake `system.ping` is never answered: bridge stays `down` with a "did not answer handshake" reason |
| Plugin never answers a call | `LR_TIMEOUT` + `cancelCommand` |
| Daemon crash | task restarts it (1 min); the next `lrc` call also starts it; in-flight calls are lost |
| Port 54330 taken | daemon logs and exits 1 |
| Stale `lrd.json` after a crash | the client gets connection refused, self-heals (Windows) or reports `DAEMON_DOWN` |

There is no idle heartbeat (the design makes it conditional on a live probe showing idle clients are dropped).
If the plugin drops an idle link, the daemon simply sees EOF and reconnects.

## Tests

`python -m pytest tests/daemon` (about 20 s; `LRD_SOAK_N` sets the soak size, default 10 000). The mock plugin
(`tests/daemon/mock_plugin.py`) reproduces the single-client sockets, restart-on-close, unserviced backlog clients,
latency/drops/out-of-order replies and `server.shutdown`. No test touches a real Lightroom.
