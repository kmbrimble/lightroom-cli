"""Stateful catalog on top of the daemon MockPlugin (one-connection semantics, port file, kill_connection)."""
import csv
import os

from tests.daemon.mock_plugin import MockPlugin

FOCUS = ("NA900001.ARW", "NA900002.ARW", "NA900003.ARW")


class CatalogMock(MockPlugin):
    def __init__(self, photos, **kw):
        super().__init__(**kw)
        self.cat = dict(photos)          # id -> filename
        self.removed = []
        self.cleared = []
        self.no_clear = False            # develop.clearAutoTone -> UNKNOWN_COMMAND
        self.gone_on_remove = set()      # ids that answer PHOTO_NOT_FOUND although listed
        self.drop_after_removes = None   # kill the link after N successful removals
        self.clear_skip = {}             # id -> skippedReason

    async def _handle(self, msg):
        cmd, p, mid = msg.get("command"), msg.get("params") or {}, msg.get("id")
        err = lambda code: self._reply(mid, False, error={"code": code, "message": code, "severity": "error", "details": {}})  # noqa: E731
        self.received.append((cmd, p))
        if cmd == "system.ping":
            return await self._reply(mid, True, {"pong": True})
        if cmd == "catalog.getAllPhotos":
            ids = sorted(self.cat)
            o, n = p.get("offset", 0), p.get("limit", 1000)
            page = [{"id": i, "filename": self.cat[i]} for i in ids[o:o + n]]
            return await self._reply(mid, True, {"photos": page, "total": len(ids), "offset": o, "limit": n, "returned": len(page)})
        if cmd == "catalog.getPhotoMetadata":
            i = int(p["photoId"])
            return await (self._reply(mid, True, {"id": i, "filename": self.cat[i]}) if i in self.cat else err("PHOTO_NOT_FOUND"))
        if cmd == "catalog.removeFromCatalog":
            i = int(p["photoId"])
            self.writes.append((cmd, p))
            if i not in self.cat or i in self.gone_on_remove:
                self.cat.pop(i, None)
                return await err("PHOTO_NOT_FOUND")
            del self.cat[i]
            self.removed.append(i)
            await self._reply(mid, True, {"photoId": i})
            if self.drop_after_removes is not None and len(self.removed) == self.drop_after_removes:
                self.drop_after_removes = None
                self.kill_connection()
            return
        if cmd == "develop.clearAutoTone":
            if self.no_clear:
                return await err("UNKNOWN_COMMAND")
            self.writes.append((cmd, p))
            self.cleared += p["photoIds"]
            out = [{"id": i, "filename": self.cat[i],
                    "changed": {} if i in self.clear_skip else {"Exposure2012": {"before": 1.0, "after": 0.0}},
                    "skippedReason": self.clear_skip.get(i)} for i in p["photoIds"]]
            return await self._reply(mid, True, {"results": out})
        await err("UNKNOWN_COMMAND")


def make_fixture(d):
    """Scaled-down catalog in the real CSV schema. Returns (expected, photos)."""
    sizes = [3, 5, 3, 2, 5, 3]            # sets 1-6 merged; set 6 has -HDR and -HDR-2
    unmerged = [2, 3]                     # sets 7-8
    photos, hdr, sets, nid, num = {}, [], [], 1000, 100000
    def new(name):
        nonlocal nid
        nid += 1
        photos[nid] = name
        return nid
    for sn, size in enumerate(sizes + unmerged, 1):
        nums = list(range(num, num + size)); num += size + 3
        sids = [new(f"NA{n}.ARW") for n in nums]
        mids = []
        if sn <= len(sizes):
            mids = [new(f"NA{nums[0]}-HDR.dng")]
            if sn == 6:
                mids.insert(0, new(f"NA{nums[0]}-HDR-2.dng"))
        for m in mids:
            hdr.append((m, photos[m]))
        sets.append(dict(set_id=sn, n_frames=size, file_numbers=" ".join(map(str, nums)), n_merges=len(mids),
                         merge_ids=" ".join(map(str, mids)), source_ids=" ".join(map(str, sids)), kw="Exposure bracket"))
    for n in [1, 2, 3, 4]:
        new(f"NA8000{n}.ARW")
    for n in FOCUS:
        new(n)
    dup = [(i, n) for i, n in hdr if n.endswith("-HDR-2.dng")] + [(i, n) for i, n in hdr if n == f"NA{sets[5]['file_numbers'].split()[0]}-HDR.dng"]
    def write(name, cols, rows):
        with open(os.path.join(d, name), "w", newline="") as f:
            w = csv.DictWriter(f, cols); w.writeheader(); w.writerows(rows)
    write("hdr_outputs.csv", ["id", "filename"], [dict(id=i, filename=n) for i, n in hdr])
    write("bracket_sets.csv", list(sets[0]), sets)
    write("duplicate_merges.csv", ["group_set_id", "id", "filename"], [dict(group_set_id=6, id=i, filename=n) for i, n in dup])
    exp = dict(total=len(photos), hdr=len(hdr), groups=1, sets=8, merged_sets=6, unmerged_sets=2,
               unmerged_arws=sum(unmerged), sources=sum(sizes), focus=FOCUS)
    return exp, photos
