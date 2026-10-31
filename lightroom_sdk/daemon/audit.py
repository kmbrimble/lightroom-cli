"""JSON-lines audit log of every non-read call, rotated at 5 MB (audit.jsonl -> audit.jsonl.1)."""

import hashlib
import json
import os
import time

MAX_BYTES = 5 * 1024 * 1024


def digest(params) -> str:
    raw = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class AuditLog:
    def __init__(self, path, max_bytes=MAX_BYTES):
        self.path = str(path)
        self.max_bytes = max_bytes

    def write(self, client, command, params, affected, decision, outcome, **extra):
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "client": client,
            "command": command,
            "params_digest": digest(params),
            "affected": affected,
            "decision": decision,
            "outcome": outcome,
        }
        rec.update(extra)
        line = json.dumps(rec, default=str) + "\n"
        try:
            try:
                if os.path.getsize(self.path) + len(line) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")  # ponytail: one generation kept
            except OSError:
                pass
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # never let audit I/O fail a call; the daemon log records nothing sensitive either
