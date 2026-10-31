"""Review/Fix keywords from the whole-catalog Sonnet quality labels (offline, pure; see docs/triage-keywords.md)."""
import json

R = "Review - "
F = "Fix - "
NAMES = {  # keyword -> smart collection name
    R + "Blown highlights": "AI Review - Blown highlights", R + "Crushed shadows": "AI Review - Crushed shadows",
    R + "Too dark": "AI Review - Too dark", R + "Too bright": "AI Review - Too bright", R + "Soft focus": "AI Review - Soft focus",
    R + "Motion blur": "AI Review - Motion blur", R + "Needs straightening": "AI Review - Needs straightening",
    R + "Reject candidate": "AI Review - Reject candidate", R + "Top pick": "AI Review - Top picks",
    R + "Uncertain": "AI Review - Uncertain", R + "Possibly intentional": "AI Review - Possibly intentional",
    F + "Lower highlights": "AI Review - Fix lower highlights", F + "Lift shadows": "AI Review - Fix lift shadows",
    F + "Raise exposure": "AI Review - Fix raise exposure", F + "Lower exposure": "AI Review - Fix lower exposure",
    F + "Sharpen": "AI Review - Fix sharpen", F + "Crop": "AI Review - Fix crop", F + "Straighten": "AI Review - Fix straighten",
}


def review_keywords(r, canyon=False):
    fx, ks = set(r["fixes"]), r["keeper_score"]
    low = ks <= 3  # weak photo: keep its fix hints even when the matching severity is mild
    tilt = r.get("horizon_tilt_deg")
    tilted = tilt is not None and abs(tilt) >= 1 and (r.get("tilt_confidence") or 0) >= 0.6
    k = []
    flags = [
        ("Blown highlights", r["highlights"] >= 2), ("Crushed shadows", r["shadows"] >= 2),
        ("Too dark", (r["exposure"] <= -1 and bool(fx & {"raise_exposure", "lift_shadows"})) or r["exposure"] <= -2),
        ("Too bright", r["exposure"] >= 2 or "lower_exposure" in fx), ("Soft focus", r["focus"] >= 2),
        ("Motion blur", r["motion_blur"] >= 2), ("Needs straightening", tilted),
        ("Reject candidate", ks <= 2), ("Top pick", ks == 5), ("Uncertain", bool(r["uncertain"])),
    ]
    k += [R + n for n, on in flags if on]
    if canyon and ("Motion blur" in [n for n, on in flags if on] or "Blown highlights" in [n for n, on in flags if on]):
        k.append(R + "Possibly intentional")
    hints = [
        ("lower_highlights", "Lower highlights", r["highlights"] >= 2 or low), ("lift_shadows", "Lift shadows", r["shadows"] >= 2 or low),
        ("raise_exposure", "Raise exposure", r["exposure"] <= -1 or low), ("lower_exposure", "Lower exposure", r["exposure"] >= 1 or low),
        ("sharpen", "Sharpen", r["focus"] >= 2 or r["motion_blur"] >= 2 or low), ("crop", "Crop", low),
        ("straighten", "Straighten", tilted or low),
    ]
    k += [F + n for code, n, on in hints if code in fx and on]
    return k


def plan(labels, triage_plan):
    canyon = {p["photo_id"] for p in triage_plan if {"canyon", "slot canyon"} & set(p["keywords"])}
    out = []
    for r in labels:
        ks = review_keywords(r, str(r["id"]) in canyon)
        if ks:
            out.append({"photo_id": str(r["id"]), "keywords": ks})
    return out


def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


# Smart collections: Lightroom's 'any' operator splits the value into words and matches ANY of them, so the full keyword
# ("Review - Top pick") matches every Review keyword. Each collection therefore uses ONE word that occurs in no other keyword.
# Fix - * hints get no collection (their words are shared with Review keywords).
TOKENS = {R + "Blown highlights": "Blown", R + "Crushed shadows": "Crushed", R + "Too dark": "dark", R + "Too bright": "bright",
          R + "Soft focus": "Soft", R + "Motion blur": "blur", R + "Needs straightening": "straightening",
          R + "Reject candidate": "Reject", R + "Top pick": "pick", R + "Uncertain": "Uncertain",
          R + "Possibly intentional": "Possibly"}


def collections():
    return [(NAMES[k], t) for k, t in TOKENS.items()]
