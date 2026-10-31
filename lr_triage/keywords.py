"""Turn final.ndjson triage rows into per-photo Lightroom keyword lists (offline, pure; see docs/triage-keywords.md).

Never emits problem/quality keywords (visible_problems, tilted_horizon, lr_quality flags).
"""
import collections
import json

MARKER = "AI tagged"
MIN_PHOTOS = 3
MAX_SUBJECT = 8

SCENE_KW = {
    "mountain_landscape": ["mountain"], "lake": ["lake"], "desert": ["desert"], "waterfall": ["waterfall"],
    "wildlife": ["wildlife"], "coast_beach": ["coast"], "architecture": ["architecture"], "forest": ["forest"],
    "garden_flowers": ["garden"], "city_skyline": ["city"], "indoor": ["indoor"], "people_portrait": ["portrait"],
    "sky_clouds": ["sky"], "street": ["street"], "vehicle": ["vehicle"], "other": [],
}
# 'landscape' is kept only where it is not implied by the scene keyword
LANDSCAPE_SCENES = {"mountain_landscape", "lake", "desert", "waterfall", "coast_beach", "forest", "sky_clouds", "garden_flowers"}

SYN = {  # raw (lowercase) -> canonical
    "clouds": "cloud", "trees": "tree", "rocks": "rock", "mountains": "mountain", "hoodoos": "hoodoo", "flowers": "flower",
    "hills": "hill", "canon": "canyon", "fall": "autumn", "blue sky": "sky", "cloudy": "overcast", "evergreen": "conifer",
    "conifers": "conifer", "pine trees": "pine", "hikers": "hiker", "hike": "hiking", "shrubs": "shrub", "tourists": "tourist",
    "visitor": "tourist", "grizzly": "grizzly bear", "seagulls": "seagull", "peaks": "peak", "cliffs": "cliff",
    "logs": "log", "boulders": "boulder", "umbrellas": "umbrella", "cars": "car", "suspension": "suspension bridge",
    "golden gate": "golden gate bridge", "sunburst": "sunstar", "people": "people", "person": "people", "rock formations": "rock formation",
}
DROP = {  # meta words, colours-only, mood-only, technical/quality, people appearance (comma list, one phrase each)
    *"landscape,nature,outdoor,outdoors,travel,scenic,natural,view,wild,day,color,colorful,clear,distant,dramatic,minimalist,"
     "adventure,wide angle,shallow depth of field,bokeh,blur,blown highlights,low light,low key,looking up,smartphone,"
     "blue,red,green,orange,purple,pink,golden,turquoise,yellow,white,black,brown,gray,grey,warm,warm tones,warm light,orange sky,"
     "calm,moody,still,dark,serene,peaceful,light,sun,cold morning,puffer jacket,beanie,long hair,smiling,smile,selfie,woman,man,"
     "tilted horizon,tilted_horizon".split(",")}
SPECIES = {"bear", "grizzly bear", "brown bear", "deer", "squirrel", "marmot", "wolf", "rodent", "animal", "bird", "seagull",
           "heron", "duck", "flamingo", "fish", "sea lion", "bee", "wombat", "moose", "elk", "goat", "sheep", "eagle", "bison"}
CANYON_WORDS = ("canyon", "canon")


def norm(k):
    k = " ".join(str(k).lower().replace("_", " ").split())
    return SYN.get(k, k)


def _cands(row):
    f = row["fields"]
    out = [norm(k) for k in f["keywords"]]
    return [k for k in out if k not in DROP or k == "landscape"]


def _folds(rows):
    """plural/singular merge for anything not in SYN: X+'s' -> X when X is itself a keyword."""
    seen = {k for r in rows for k in _cands(r)}
    return {k: k[:-1] for k in seen if k.endswith("s") and k[:-1] in seen}


def build_vocab(rows):
    """canonical keyword -> number of photos carrying it (after normalisation and folding, before thresholds)."""
    fold, c = _folds(rows), collections.Counter()
    for r in rows:
        c.update({fold.get(k, k) for k in _cands(r)})
    return c


def plan(rows):
    """-> (list of {photo_id, keywords}, dropped-counter). Deterministic."""
    vocab, fold = build_vocab(rows), _folds(rows)
    out, dropped = [], collections.Counter()
    for r in rows:
        f = r["fields"]
        scene = f["scene_type"]
        kws = list(SCENE_KW.get(scene, []))
        blob = " ".join(f["keywords"] + f["subjects"]).lower()
        if any(w in blob for w in CANYON_WORDS) and "canyon" not in kws:
            kws.append("canyon")
        if f["has_people"]:
            kws.append("people")
            if f["people_count"] >= 4:
                kws.append("group")
        local_unsure = r["source"] == "local" and r.get("min_conf", 1.0) < 0.6
        subj = []
        for k in _cands(r):
            k = fold.get(k, k)
            if k == "landscape" and scene in LANDSCAPE_SCENES:
                dropped["landscape(redundant)"] += 1; continue
            if k == "people" or k in kws or k in subj:
                continue
            if vocab[k] < MIN_PHOTOS:
                dropped["rare"] += 1; continue
            if local_unsure and k in SPECIES:
                dropped["unconfirmed species"] += 1; continue
            subj.append(k)
        kws += subj[:MAX_SUBJECT]
        dropped["over cap"] += max(0, len(subj) - MAX_SUBJECT)
        out.append({"photo_id": str(r["photo_id"]), "keywords": kws + [MARKER]})
    return out, dropped


def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
