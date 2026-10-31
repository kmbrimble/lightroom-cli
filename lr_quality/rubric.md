# lr_quality ground-truth rubric (quality-labels/1)

Judged as a landscape/travel photographer deciding what needs fixing in Lightroom. Deliberate artistic choices
(silhouettes, low-key or high-key mood, sun in frame, small flares) are not defects. Labels are calibrated, not harsh.
The reviewer never sees lr_quality output, so labels are independent of the analyser.

| field | values | meaning |
|---|---|---|
| highlights | 0-3 | 0 none; 1 minor clipped areas, not distracting; 2 noticeable/distracting (large blown sky or bright area); 3 severe |
| shadows | 0-3 | crushed/blocked-up shadow detail that matters (0 none ... 3 severe) |
| exposure | -3..+3 | -3 far too dark, 0 right, +3 far too bright |
| focus | 0-3 | 0 sharp where it matters; 1 slightly soft; 2 clearly soft/out of focus; 3 unusable |
| motion_blur | 0-3 | camera shake or subject blur |
| noise | 0-3 | visible noise/grain at this size |
| horizon_tilt_deg | number or null | degrees the image must be rotated **clockwise** to level the horizon or dominant level line; null if no reliable horizon |
| tilt_confidence | 0-1 | confidence in horizon_tilt_deg |
| keeper_score | 1-5 | overall technical keeper rating (5 excellent, 1 reject) |
| fixes | subset of lower_highlights, lift_shadows, raise_exposure, lower_exposure, straighten, sharpen, crop, none | what to do in Lightroom |
| uncertain | bool | reviewer is unsure about any field |
| note | string | one line |

**Sign convention vs lr_quality:** `tilt.angle_deg > 0` means the horizon descends to the right and a counter-clockwise
rotation fixes it. So the label equivalent is `horizon_tilt_deg ~= -tilt.angle_deg`.
