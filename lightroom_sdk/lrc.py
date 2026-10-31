"""lrc: thin stdlib-only client for the lrd daemon. Run as a FILE, never via -m / console_script:

    python -S -E lightroom_sdk/lrc.py call catalog.getSelectedPhotos
    python -S -E lightroom_sdk/lrc.py call develop.setValue '{"parameter":"Exposure","value":0.5}' --timeout 20
    python -S -E lightroom_sdk/lrc.py health | stop | ping | commands | batch FILE|-

Exit codes: 0 ok, 1 Lightroom/other error, 2 guard denied, 3 bridge/daemon down, 4 timeout.
Imports only sys/os/json/socket (subprocess lazily, Windows self-heal only). Do NOT import lightroom_sdk here.
"""
import json
import os
import socket
import sys
import time

TASK = "LightroomCliDaemon"
EXIT = {"GUARD_DENIED": 2, "BRIDGE_DOWN": 3, "BRIDGE_LOST": 3, "BRIDGE_BUSY": 3, "DAEMON_DOWN": 3, "LR_TIMEOUT": 4}


def home_dir():
    h = os.environ.get("LRD_HOME")
    if h:
        return h
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "lightroom-cli")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "lightroom-cli")


def load_state():
    try:
        with open(os.path.join(home_dir(), "lrd.json")) as f:
            s = json.load(f)
        return int(s["port"]), s["token"]
    except (OSError, ValueError, KeyError):
        return None


class Down(Exception):
    pass


def http(method, path, body, timeout, state):
    port, token = state
    data = json.dumps(body).encode() if body is not None else b""
    head = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Lrd-Token: {token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=min(timeout, 3))
    except OSError as e:
        raise Down(str(e))
    try:
        s.settimeout(timeout)
        s.sendall(head + data)
        buf = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            buf += c
    except socket.timeout:
        return 0, {"ok": False, "error": {"code": "LR_TIMEOUT", "message": "no reply from daemon in time"}}
    except OSError as e:
        raise Down(str(e))
    finally:
        s.close()
    h, _, b = buf.partition(b"\r\n\r\n")
    try:
        return int(h.split(b" ", 2)[1]), json.loads(b)
    except (ValueError, IndexError):
        raise Down("garbled reply from daemon")


def start_daemon():
    """Self-heal: only on Windows, only via the scheduled task (never spawn directly: sshd leaks children)."""
    if sys.platform != "win32":
        return
    import subprocess

    try:
        subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, timeout=10, creationflags=0x08000000)
    except Exception:
        return
    end = time.time() + 3
    while time.time() < end:
        st = load_state()
        if st:
            try:
                if http("GET", "/v1/health", None, 1, st)[0] == 200:
                    return
            except Down:
                pass
        time.sleep(0.1)


def request(method, path, body, timeout, heal=True):
    """-> (status, obj). Re-reads the token once on 401; starts the daemon once if unreachable."""
    for attempt in range(3):
        st = load_state()
        try:
            if st is None:
                raise Down("no lrd.json")
            code, obj = http(method, path, body, timeout, st)
            if code == 401 and attempt == 0:
                continue
            return code, obj
        except Down as e:
            if heal and attempt == 0:
                start_daemon()
                heal = False
                continue
            return 0, {"ok": False, "error": {"code": "DAEMON_DOWN", "message": f"lrd not reachable: {e}",
                                              "details": {"hint": "schtasks /run /tn " + TASK}}}
    return 0, {"ok": False, "error": {"code": "DAEMON_DOWN", "message": "lrd rejected the token"}}


def exit_code(obj):
    if obj.get("ok"):
        return 0
    if "results" in obj:
        for r in obj["results"]:
            if not r.get("ok"):
                return exit_code(r)
    return EXIT.get((obj.get("error") or {}).get("code"), 1)


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    return exit_code(obj)


def parse(args):
    pos, opt, flags = [], {}, {}
    it = iter(args)
    for a in it:
        if a in ("--timeout", "--client", "--wait-bridge", "--request-id"):
            opt[a[2:].replace("-", "_")] = next(it, "")
        elif a == "--allow-remove-from-catalog":
            flags["allow_remove_from_catalog"] = True
        elif a == "--allow-external-edit":
            flags["allow_external_edit"] = True
        else:
            pos.append(a)
    return pos, opt, flags


def build_call(command, raw, opt, flags):
    try:
        params = json.loads(raw) if raw else {}
    except ValueError as e:
        raise SystemExit(emit({"ok": False, "error": {"code": "BAD_REQUEST", "message": f"params not JSON: {e}"}}) or 1)
    body = {"command": command, "params": params, "client": opt.get("client") or os.environ.get("LRC_CLIENT", "lrc")}
    if "timeout" in opt:
        body["timeout"] = float(opt["timeout"])
    if "wait_bridge" in opt:
        body["wait_bridge"] = float(opt["wait_bridge"])
    if "request_id" in opt:
        body["request_id"] = opt["request_id"]
    if flags:
        body["flags"] = flags
    return body


def main(argv):
    pos, opt, flags = parse(argv)
    if not pos:
        sys.stderr.write(__doc__)
        return 1
    cmd, rest = pos[0], pos[1:]
    if cmd in ("call", "ping"):
        if cmd == "ping":
            rest = ["system.ping"] + rest
        if not rest:
            sys.stderr.write("usage: lrc call <command> [json-params]\n")
            return 1
        body = build_call(rest[0], rest[1] if len(rest) > 1 else "", opt, flags)
        t = float(opt.get("timeout") or 120) + 15
        _, obj = request("POST", "/v1/call", body, t)
    elif cmd == "batch":
        src = rest[0] if rest else "-"
        calls = json.load(sys.stdin if src == "-" else open(src))
        if isinstance(calls, dict):
            calls = calls.get("calls", [])
        client = opt.get("client") or "lrc"
        for c in calls:
            c.setdefault("client", client)
        _, obj = request("POST", "/v1/batch", {"calls": calls, "ordered": True}, 600)
    elif cmd == "health":
        _, obj = request("GET", "/v1/health", None, 5)
    elif cmd == "commands":
        _, obj = request("GET", "/v1/commands", None, 5)
    elif cmd == "stop":
        _, obj = request("POST", "/v1/shutdown", {}, 10, heal=False)
        if obj.get("error", {}).get("code") == "DAEMON_DOWN":
            obj = {"ok": True, "stopping": False, "note": "daemon was not running"}
    else:
        sys.stderr.write(f"unknown subcommand {cmd!r}\n")
        return 1
    return emit(obj)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
