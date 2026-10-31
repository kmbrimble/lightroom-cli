#!/usr/bin/env python3
"""One-off, resumable Lightroom catalog clean-up (see docs/catalog-cleanup.md).

A: remove the `-HDR-2.dng` duplicate merges.  B: remove the bracket SOURCE ARWs of every set that still has
a merge.  C: develop.clearAutoTone on everything left.  Photos leave the CATALOG only; this script has no
code path that touches files on disk (it writes one append-only NDJSON log, nothing else).

Exit codes: 0 ok / dry run, 1 runtime failure, 2 plan or preflight assertion failed, 3 step C unavailable.
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

from lightroom_sdk.client import LightroomClient
from lightroom_sdk.exceptions import (
    ConnectionError as LRConnectionError,
    LightroomSDKError,
    PhotoNotFoundError,
    TimeoutError as LRTimeoutError,
)

# Verified from the dry-run CSVs. NB: the original brief said 4,945 sources; that wrongly counts the 487 never
# stacked single ARWs. Sources of merged sets are 4,458 (949 sets, no ARW shared between sets).
EXPECTED = dict(total=6007, hdr=975, groups=26, sets=972, merged_sets=949, unmerged_sets=23,
                unmerged_arws=84, sources=4458, focus=("NA107829.ARW", "NA107830.ARW", "NA107831.ARW"))
SAMPLE = 20
BATCH = 25
PAGE = 200  # getAllPhotos reads 7 metadata fields per photo; keep pages small enough for the 30 s timeout
TIMEOUT = 30.0
BACKOFF = (1, 2, 4, 8, 16, 30)
# The only bridge commands this script may ever send. None can delete a file.
ALLOWED = frozenset({"system.ping", "catalog.getAllPhotos", "catalog.getPhotoMetadata",
                     "catalog.removeFromCatalog", "develop.clearAutoTone"})
DONE = {"removed", "already_gone", "cleared", "unchanged", "skipped"}


class Abort(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


class StepCUnavailable(Exception):
    pass


# ---------------------------------------------------------------- plan from CSVs
class Plan:
    pass


def _rows(csv_dir, name):
    with open(os.path.join(csv_dir, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _need(cond, msg):
    if not cond:
        raise Abort("PLAN ASSERTION FAILED: " + msg, 2)


def load_plan(csv_dir, exp=EXPECTED):
    hdr = _rows(csv_dir, "hdr_outputs.csv")
    sets = _rows(csv_dir, "bracket_sets.csv")
    dups = _rows(csv_dir, "duplicate_merges.csv")
    name = {int(r["id"]): r["filename"] for r in hdr}
    _need(len(hdr) == exp["hdr"] == len(name), f"hdr_outputs rows {len(hdr)} (unique ids {len(name)}), want {exp['hdr']}")
    _need(len(sets) == exp["sets"], f"bracket_sets rows {len(sets)}, want {exp['sets']}")
    is2 = lambda n: n.lower().endswith("-hdr-2.dng")  # noqa: E731
    _need(all(n.lower().endswith("-hdr.dng") or is2(n) for n in name.values()), "unexpected merge filename")

    # step A: one -HDR-2.dng per duplicate group, partner -HDR.dng must exist
    groups = {}
    for r in dups:
        groups.setdefault(r["group_set_id"], []).append((int(r["id"]), r["filename"]))
    _need(len(groups) == exp["groups"], f"duplicate groups {len(groups)}, want {exp['groups']}")
    plan = Plan()
    plan.dups = []
    for g, members in sorted(groups.items(), key=lambda kv: int(kv[0])):
        _need(len(members) == 2, f"group {g} has {len(members)} members")
        _need(all(name.get(i) == n for i, n in members), f"group {g} ids/filenames disagree with hdr_outputs")
        two = [m for m in members if is2(m[1])]
        one = [m for m in members if not is2(m[1])]
        _need(len(two) == 1 and len(one) == 1 and one[0][1].lower()[:-8] == two[0][1].lower()[:-10],
              f"group {g} is not an -HDR/-HDR-2 pair: {members}")
        plan.dups.append((two[0], one[0]))  # ((id, name) to remove, (id, name) partner)
    removed_a = {t[0][0] for t in plan.dups}
    _need(len(removed_a) == exp["groups"], "duplicate -HDR-2 ids not unique")

    # step B: sources of every set that has a surviving merge
    plan.sets, kept, seen = [], [], set()
    n_merged = 0
    for s in sets:
        _need("focus" not in s["kw"].lower(), f"set {s['set_id']} has a Focus bracket keyword")
        mids = [int(x) for x in s["merge_ids"].split()]
        sids = [int(x) for x in s["source_ids"].split()]
        nums = s["file_numbers"].split()
        _need(len(mids) == int(s["n_merges"]) and len(sids) == len(nums) == int(s["n_frames"]),
              f"set {s['set_id']} id/frame counts inconsistent")
        names = [f"NA{n}.ARW" for n in nums]
        _need(all(x.lower().endswith(".arw") for x in names), f"set {s['set_id']} non-ARW source")
        _need(not (set(sids) & set(name)), f"set {s['set_id']} source id is a merge id")
        _need(not (set(sids) & seen) and len(set(sids)) == len(sids), f"set {s['set_id']} source id in several sets")
        _need(not set(names) & set(exp["focus"]), f"set {s['set_id']} contains a focus-bracket frame")
        seen |= set(sids)
        src = list(zip(sids, names))
        if not mids:
            kept += src
            continue
        n_merged += 1
        _need(all(m in name for m in mids), f"set {s['set_id']} merge id not in hdr_outputs")
        surv = [(m, name[m]) for m in mids if m not in removed_a]
        _need(len(surv) >= 1 and all(not is2(n) for _, n in surv), f"set {s['set_id']} has no surviving merge after A")
        plan.sets.append((int(s["set_id"]), surv, src))
    plan.kept = kept
    plan.sources = [x for _, _, src in plan.sets for x in src]
    _need(n_merged == exp["merged_sets"], f"merged sets {n_merged}, want {exp['merged_sets']}")
    _need(len(sets) - n_merged == exp["unmerged_sets"], f"unmerged sets {len(sets) - n_merged}, want {exp['unmerged_sets']}")
    _need(len(kept) == exp["unmerged_arws"], f"ARWs of merge-less sets {len(kept)}, want {exp['unmerged_arws']}")
    _need(len(plan.sources) == exp["sources"], f"sources to remove {len(plan.sources)}, want {exp['sources']}")
    _need(sum(1 for _, s_, _ in plan.sets if len(s_) == 1) == exp["merged_sets"], "a set lost its only merge")
    plan.exp = exp
    plan.after_a = exp["total"] - exp["groups"]
    plan.after_b = plan.after_a - exp["sources"]
    return plan


# ---------------------------------------------------------------- audit log
class Log:
    def __init__(self, path, enabled):
        self.path, self.enabled, self.done, self.fh = path, enabled, {}, None
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines):
                try:
                    r = json.loads(line)
                except ValueError:
                    if i == len(lines) - 1:
                        continue  # torn final line from a crash
                    raise Abort(f"log {path} is corrupt at line {i + 1}", 2)
                if r.get("result") in DONE and r.get("id") is not None:
                    self.done.setdefault(r["step"], {})[int(r["id"])] = r["result"]
        if enabled:
            self.fh = open(path, "a", encoding="utf-8")

    def is_done(self, step, pid):
        return pid in self.done.get(step, {})

    def removed_count(self):
        return sum(1 for s in "AB" for v in self.done.get(s, {}).values() if v == "removed")

    def write(self, step, pid, filename, action, result, **extra):
        if result in DONE and pid is not None:
            self.done.setdefault(step, {})[int(pid)] = result
        if self.fh:
            rec = dict(ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"), step=step, id=pid,
                       filename=filename, action=action, result=result, **extra)
            self.fh.write(json.dumps(rec) + "\n")
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def close(self):
        if self.fh:
            self.fh.close()


# ---------------------------------------------------------------- runner
class Runner:
    def __init__(self, client, log, out=print, sleep=asyncio.sleep, backoff=BACKOFF):
        self.client, self.log, self.out, self.sleep, self.backoff = client, log, out, sleep, backoff
        self.live = {}
        self.stats = {s: Counter() for s in "ABC"}

    async def call(self, cmd, params=None):
        assert cmd in ALLOWED, cmd
        tries = 0
        while True:
            reconnect = True
            try:
                return await self.client.execute_command(cmd, params, timeout=TIMEOUT)
            except LRTimeoutError:
                reconnect = False  # link is fine, plugin is slow; commands used here are idempotent
            except LRConnectionError:
                pass
            except LightroomSDKError as e:
                if e.code != "WRITE_ABORTED":
                    raise
                reconnect = False
            except (OSError, asyncio.TimeoutError):
                pass
            tries += 1
            if tries > len(self.backoff):
                raise Abort(f"giving up on {cmd} after {tries} attempts (link or plugin not responding)")
            self.out(f"  ! {cmd} failed, retry {tries}/{len(self.backoff)} in {self.backoff[tries - 1]}s")
            await self.sleep(self.backoff[tries - 1])
            if reconnect:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                try:
                    await self.client.connect()
                except Exception:
                    pass  # next loop iteration retries (and backs off) again

    async def enumerate_catalog(self):
        photos, offset = {}, 0
        while True:
            r = await self.call("catalog.getAllPhotos", {"limit": PAGE, "offset": offset})
            total, page = r["total"], r["photos"]
            photos.update({int(p["id"]): p["filename"] for p in page})
            offset += len(page)
            if not page or offset >= total:
                break
        if len(photos) != total:
            raise Abort(f"catalog enumeration returned {len(photos)} photos but total={total}")
        return photos

    async def remove(self, step, pid, fname):
        try:
            await self.call("catalog.removeFromCatalog", {"photoId": pid})
            res = "removed"
        except PhotoNotFoundError:
            res = "already_gone"
        except LightroomSDKError as e:
            self.log.write(step, pid, fname, "removeFromCatalog", "error", error=e.code)
            raise Abort(f"removeFromCatalog {pid} ({fname}) failed: {e.code} {e}")
        self.log.write(step, pid, fname, "removeFromCatalog", res)
        self.live.pop(pid, None)
        self.stats[step][res] += 1

    async def meta_name(self, pid):
        try:
            return (await self.call("catalog.getPhotoMetadata", {"photoId": pid})).get("filename")
        except PhotoNotFoundError:
            return None

    # -- preflight
    async def preflight(self, plan, steps, dry):
        exp = plan.exp
        await self.call("system.ping")
        self.live = await self.enumerate_catalog()
        total = len(self.live)
        resumed = bool(self.log.done)
        n_logged = self.log.removed_count()
        planned = exp["groups"] + exp["sources"]
        if resumed:
            ok = exp["total"] - planned <= total <= exp["total"] - n_logged
        else:
            ok = total == exp["total"]
        if not ok:
            raise Abort(f"PREFLIGHT: live catalog has {total} photos; expected "
                        f"{'between %d and %d (resumed run, %d removals logged)' % (exp['total'] - planned, exp['total'] - n_logged, n_logged) if resumed else exp['total']}", 2)
        todo = {"A": [t[0] for t in plan.dups], "B": plan.sources}
        for step in "AB":
            if step not in steps:
                continue
            pending = [(i, n) for i, n in todo[step] if not self.log.is_done(step, i)]
            for i, n in pending:
                if i in self.live and self.live[i] != n:
                    raise Abort(f"PREFLIGHT: id {i} is {self.live[i]!r} in the catalog, CSV says {n!r}", 2)
                if i not in self.live and not resumed:
                    raise Abort(f"PREFLIGHT: id {i} ({n}) not in the catalog on a fresh run", 2)
            stride = max(1, len(pending) // SAMPLE)
            for i, n in pending[::stride][:SAMPLE]:
                got = await self.meta_name(i)
                if got is None and not resumed or got is not None and got != n:
                    raise Abort(f"PREFLIGHT: getPhotoMetadata({i}) -> {got!r}, CSV says {n!r}", 2)
        keep = list(plan.kept) + [x for _, s, _ in plan.sets for x in s] + [t[1] for t in plan.dups]
        for i, n in keep:
            if self.live.get(i) != n:
                raise Abort(f"PREFLIGHT: photo to KEEP {i} ({n}) is {self.live.get(i)!r} in the catalog", 2)
        for n in exp["focus"]:
            if n not in self.live.values():
                raise Abort(f"PREFLIGHT: focus-bracket frame {n} not found in the catalog", 2)
        self.out(f"preflight ok: live catalog {total} photos; all kept photos present; "
                 f"{SAMPLE}-id metadata samples match for steps {''.join(s for s in steps if s in 'AB')}")

    def print_plan(self, plan, steps, dry):
        e = plan.exp
        self.out("PLAN" + ("  (DRY RUN - nothing will be written)" if dry else ""))
        self.out(f"  catalog now            : {e['total']} photos ({e['hdr']} merged DNG, {e['total'] - e['hdr']} ARW)")
        self.out(f"  A remove -HDR-2.dng    : {e['groups']} (partner -HDR.dng kept)")
        self.out(f"  B remove bracket ARWs  : {e['sources']} from {e['merged_sets']} merged sets")
        self.out(f"    kept: {e['unmerged_arws']} ARWs of {e['unmerged_sets']} merge-less sets, {len(e['focus'])} focus-bracket frames, "
                 f"single ARWs")
        self.out(f"  C develop.clearAutoTone: all {plan.after_b} remaining photos, {BATCH} per call")
        self.out(f"  expected after A+B     : {plan.after_b} photos ({e['total']} - {e['groups']} - {e['sources']})")
        self.out(f"  steps selected         : {''.join(steps)}")

    # -- steps
    async def step_a(self, plan, limit):
        n = 0
        for (pid, name), (pp, pname) in plan.dups:
            if self.log.is_done("A", pid):
                self.stats["A"]["resumed"] += 1
                continue
            if limit is not None and n >= limit:
                break
            if self.live.get(pp) != pname:
                raise Abort(f"step A: partner {pname} ({pp}) of {name} is missing from the catalog; not removing")
            await self.remove("A", pid, name)
            n += 1
        self.out(f"step A: {dict(self.stats['A'])}")

    async def step_b(self, plan, limit):
        n = 0
        for set_id, surv, src in plan.sets:
            pending = [(i, nm) for i, nm in src if not self.log.is_done("B", i)]
            self.stats["B"]["resumed"] += len(src) - len(pending)
            if not pending:
                continue
            if limit is not None and n >= limit:
                break
            for sid, sname in surv[:1]:
                if await self.meta_name(sid) != sname:
                    raise Abort(f"step B: surviving merge {sname} ({sid}) of set {set_id} is not in the catalog; "
                                f"not removing its sources")
            for i, nm in pending:
                if limit is not None and n >= limit:
                    break
                await self.remove("B", i, nm)
                n += 1
                if n % 250 == 0:
                    self.out(f"  B: {n} removed so far")
        self.out(f"step B: {dict(self.stats['B'])}")

    async def step_c(self, plan, limit, force):
        live = await self.enumerate_catalog()
        gone = {i for i, _ in plan.sources} | {t[0][0] for t in plan.dups}
        if set(live) & gone and not force:
            raise Abort("step C: photos that should have been removed are still in the catalog; run A and B first", 2)
        if len(live) != plan.after_b and not force:
            raise Abort(f"step C: live catalog has {len(live)} photos, expected {plan.after_b} after A+B", 2)
        todo = [i for i in sorted(live) if not self.log.is_done("C", i)]
        self.stats["C"]["resumed"] += len(live) - len(todo)
        if limit is not None:
            todo = todo[:limit]
        for k in range(0, len(todo), BATCH):
            ids = todo[k:k + BATCH]
            try:
                r = await self.call("develop.clearAutoTone", {"photoIds": ids, "dryRun": False})
            except LightroomSDKError as e:
                if e.code == "UNKNOWN_COMMAND":
                    raise StepCUnavailable()
                raise Abort(f"develop.clearAutoTone failed: {e.code} {e}")
            items = r if isinstance(r, list) else next((v for v in r.values() if isinstance(v, list)), None)
            if items is None:
                raise Abort(f"develop.clearAutoTone: unrecognised reply shape {sorted(r)}")
            by_id = {int(x["id"]): x for x in items}
            for i in ids:
                x = by_id.get(i)
                if x is None:
                    self.log.write("C", i, live[i], "clearAutoTone", "error", error="missing from reply")
                    raise Abort(f"develop.clearAutoTone: no result for {i} ({live[i]})")
                if x.get("skippedReason"):
                    res, extra = "skipped", dict(skippedReason=x["skippedReason"])
                elif x.get("changed"):
                    res, extra = "cleared", dict(changed=x["changed"])
                else:
                    res, extra = "unchanged", {}
                self.log.write("C", i, live[i], "clearAutoTone", res, **extra)
                self.stats["C"][res] += 1
            self.out(f"  C: {min(k + BATCH, len(todo))}/{len(todo)}")
        self.out(f"step C: {dict(self.stats['C'])}")

    def step_complete(self, plan, step):
        ids = [t[0][0] for t in plan.dups] if step == "A" else [i for i, _ in plan.sources]
        return all(self.log.is_done(step, i) for i in ids)


async def run(args, client=None, out=print, **runner_kw):
    exp = args.expected if hasattr(args, "expected") else EXPECTED
    plan = load_plan(args.csv_dir, exp)
    steps = list("ABC") if args.step == "all" else [args.step]
    dry = args.dry_run or not args.yes
    log = Log(args.log, enabled=not dry)
    own = client is None
    client = client or LightroomClient(port_file=getattr(args, "port_file", None))
    r = Runner(client, log, out=out, **runner_kw)
    rc = 0
    try:
        await client.connect()
        r.print_plan(plan, steps, dry)
        await r.preflight(plan, steps, dry)
        for need, step in (("A", "B"), ("B", "C")):
            if step in steps and need not in steps and not args.force_order and not r.step_complete(plan, need):
                raise Abort(f"step {step} refused: step {need} is not complete in the log (use --force-order to override)", 2)
        if dry:
            out("dry run: no writes performed. Re-run with --yes to execute.")
        else:
            if "A" in steps:
                await r.step_a(plan, args.limit)
            if "B" in steps:
                if args.limit is None or "A" not in steps or r.step_complete(plan, "A"):
                    await r.step_b(plan, args.limit)
            if "C" in steps:
                if args.limit is not None and "B" in steps and not r.step_complete(plan, "B") and not args.force_order:
                    out("step C skipped: step B not complete (--limit run)")
                else:
                    try:
                        await r.step_c(plan, args.limit, args.force_order)
                    except StepCUnavailable:
                        out("step C NOT AVAILABLE: plugin has no develop.clearAutoTone (reload the new plugin). A/B unaffected.")
                        rc = 3
    except Abort as e:
        out(f"ABORT: {e}")
        rc = e.code
    except (LightroomSDKError, OSError) as e:
        out(f"ABORT: unexpected error: {type(e).__name__}: {e}")
        rc = 1
    finally:
        try:
            if not dry and rc in (0, 3):
                total = len(await r.enumerate_catalog())
                want = {"A": plan.after_a, "B": plan.after_b}.get(
                    "B" if "B" in steps else "A" if "A" in steps else "", None)
                out(f"POSTFLIGHT live catalog: {total} photos" + (f" (expected {want} when all of {''.join(steps)} are complete)" if want else ""))
        except Exception as e:  # postflight must never mask the real outcome
            out(f"postflight total unavailable: {e}")
        for s in "ABC":
            if r.stats[s]:
                out(f"SUMMARY {s}: " + ", ".join(f"{k}={v}" for k, v in sorted(r.stats[s].items())))
        log.close()
        if own:
            try:
                await client.disconnect()
            except Exception:
                pass
    return rc


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--csv-dir", required=True)
    p.add_argument("--step", choices=["A", "B", "C", "all"], required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, help="max write actions per step (canary runs)")
    p.add_argument("--yes", action="store_true", help="actually write; without it the run is a dry run")
    p.add_argument("--log")
    p.add_argument("--force-order", action="store_true")
    p.add_argument("--port-file")
    a = p.parse_args(argv)
    a.log = a.log or os.path.join(r"C:\NA2026\cl_scratch" if os.name == "nt" else os.getcwd(), "catalog_cleanup.ndjson")
    return a


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse())))
