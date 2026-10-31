"""Guard: the friendly early policy layer (Lua Guard.lua is the backstop).

Policy (user: keep it LIGHT):
  * the ONLY routine blocking rule: Develop writes (class DW) only on virtual copies;
  * class DR (develop reset, only develop.clearAutoTone: fixed keys -> defaults) is the one named
    exemption: allowed on originals, no VC resolve, never a read so the scheduler always audits it;
  * hard rules: default-deny unknown names, non-overridable disk-delete deny regex,
    removeFromCatalog needs flags.allow_remove_from_catalog, editInPhotoshop needs flags.allow_external_edit.
Everything else (keywords, collections, flags, ratings, setMetadata with ANY key, creating VCs) passes.
"""

import re
from dataclasses import dataclass, field

from .classes import CLASSES, READ_CLASSES

# Names that could delete files from disk. Tuned NOT to hit develop.deleteMask / removeFromCatalog /
# removeKeyword (tests assert none of the known names match). No flag can override this.
DENY_RE = re.compile(
    r"(deletefile|deletephoto|deleteoriginal|deletefrom|fromdisk|todisk|trash|recycle|unlink|rmdir|rmtree"
    r"|removefile|wipe|shred|erase|purge|harddelete|permanentl?y)",
    re.IGNORECASE,
)

HINT_VC = (
    "Develop edits are only allowed on virtual copies. Create one with catalog.createVirtualCopy "
    "(it becomes the selection), then retry."
)


@dataclass
class Verdict:
    allow: bool
    klass: str | None = None
    reason: str = ""
    hint: str = ""
    details: dict = field(default_factory=dict)
    needs_resolve: bool = False

    def error(self):
        d = {"code": "GUARD_DENIED", "message": self.reason, "details": dict(self.details)}
        if self.hint:
            d["details"]["hint"] = self.hint
        return d


def classify(command):
    return CLASSES.get(command)


def is_read(command) -> bool:
    return CLASSES.get(command) in READ_CLASSES


def precheck(command, params, flags) -> Verdict:
    """Static checks (no I/O). May return needs_resolve=True for DW."""
    flags = flags if isinstance(flags, dict) else {}
    if not isinstance(command, str) or DENY_RE.search(command):
        return Verdict(False, None, "command could delete files from disk and is never allowed")
    k = CLASSES.get(command)
    if k is None:
        return Verdict(False, None, f"unknown command {command!r} (default-deny)")
    if k == "RM" and flags.get("allow_remove_from_catalog") is not True:
        return Verdict(
            False, k, "removing photos from the catalog needs flags.allow_remove_from_catalog=true",
            hint="Catalog-only removal; files stay on disk. Pass flags.allow_remove_from_catalog=true.",
        )
    if k == "EXT" and flags.get("allow_external_edit") is not True:
        return Verdict(
            False, k, "external editing needs flags.allow_external_edit=true",
            hint="Pass flags.allow_external_edit=true (writes a derivative file next to the original).",
        )
    return Verdict(True, k, needs_resolve=(k == "DW"))


def explicit_ids(params):
    """Photo ids named in params, or None if the call acts on the current target/selection."""
    if not isinstance(params, dict):
        return None
    ids = []
    if params.get("photoIds") is not None:
        v = params["photoIds"]
        ids += list(v) if isinstance(v, (list, tuple)) else [v]
    if params.get("photoId") is not None:
        ids.append(params["photoId"])
    return ids or None


async def resolve_and_check(params, call):
    """For a DW call: return (verdict_allow, affected_ids, details).

    `call(command, params)` is an async fn returning the plugin reply dict (success/result/error).
    Absent/unknown isVirtualCopy is NOT treated as true.
    """
    ids = explicit_ids(params)
    photos = []  # (id, isVirtualCopy|None)
    if ids is None:
        r = await call("catalog.getSelectedPhotos", {})
        res = (r.get("result") or {}) if r.get("success") else None
        if res is None:
            return False, [], {"why": "could not read the selection", "plugin_error": r.get("error")}
        for p in res.get("photos") or []:
            photos.append((p.get("id"), p.get("isVirtualCopy")))
        if not photos:
            return False, [], {"why": "nothing is selected, so the target photo cannot be checked"}
    else:
        for pid in ids:
            r = await call("catalog.getPhotoMetadata", {"photoId": pid})
            res = (r.get("result") or {}) if r.get("success") else {}
            photos.append((pid, res.get("isVirtualCopy")))
    affected = [p for p, _ in photos]
    bad = [p for p, vc in photos if vc is not True]
    if bad:
        return False, affected, {"offending": bad, "why": "not a virtual copy (or could not be determined)"}
    return True, affected, {}
