SCENES = ["mountain_landscape", "lake", "forest", "waterfall", "coast_beach", "desert", "city_skyline", "street",
          "architecture", "garden_flowers", "wildlife", "vehicle", "food", "indoor", "night", "sky_clouds",
          "people_portrait", "group_photo", "other"]
PROBLEMS = ["blown_highlights", "too_dark", "blurry", "motion_blur", "tilted_horizon", "lens_flare",
            "dirty_sensor_spots", "obstruction", "none"]
# Key order matters: llama.cpp grammar emits properties in this order; label fields first.
SCHEMA = {
    "type": "object",
    "properties": {
        "scene_type": {"type": "string", "enum": SCENES},
        "has_people": {"type": "boolean"},
        "people_count": {"type": "integer", "minimum": 0},
        "is_close_up_person": {"type": "boolean"},
        "visible_problems": {"type": "array", "items": {"type": "string", "enum": PROBLEMS}, "minItems": 1, "maxItems": 4},
        "subjects": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 5, "maxItems": 12},
        "short_caption": {"type": "string", "maxLength": 140},
    },
    "required": ["scene_type", "has_people", "people_count", "is_close_up_person", "visible_problems",
                 "subjects", "keywords", "short_caption"],
    "additionalProperties": False,
}
CONF_FIELDS = ["scene_type", "has_people", "is_close_up_person", "visible_problems"]
LOW_CONF_FIELDS = ["scene_type", "has_people", "visible_problems"]  # escalation-relevant

PROMPT = (
    "You are triaging travel and landscape photos for a Lightroom catalog. Look at the image and fill in the JSON.\n"
    "- scene_type: the single best category.\n"
    "- has_people / people_count: any recognisable person, even small or distant (count only visible people, 0 if none).\n"
    "- is_close_up_person: true only if a person's face or upper body fills a large part of the frame.\n"
    "- visible_problems: only problems you can actually see (blown_highlights = large pure-white areas with no detail, "
    "too_dark, blurry, motion_blur, tilted_horizon = horizon visibly not level, lens_flare, dirty_sensor_spots, "
    "obstruction = something unwanted in front of the subject). Use [\"none\"] if the photo looks technically fine.\n"
    "- subjects: 1-5 short noun phrases.\n"
    "- keywords: 5-12 short lowercase Lightroom-style keywords (e.g. \"mountain\", \"snow\", \"sunset\"). "
    "No place names unless it is a clearly famous landmark.\n"
    "- short_caption: one plain sentence, at most 140 characters.\n"
    "scene_type must be EXACTLY one of: " + ", ".join(SCENES) + ".\n"
    "visible_problems items must be from: " + ", ".join(PROBLEMS) + ".\n"
    "Example of the exact format: {\"scene_type\": \"lake\", \"has_people\": false, \"people_count\": 0, "
    "\"is_close_up_person\": false, \"visible_problems\": [\"none\"], \"subjects\": [\"calm lake\", \"pine trees\"], "
    "\"keywords\": [\"lake\", \"reflection\", \"pine\", \"calm\", \"mountain\"], \"short_caption\": \"A calm lake at dawn.\"}\n"
    "Reply with a single JSON object with exactly these keys in this order: " + ", ".join(SCHEMA["required"]) + "."
)
