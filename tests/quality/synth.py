"""Synthetic images for lr_quality tests (no real photos)."""
import numpy as np
from PIL import Image, ImageFilter


def textured(w=1200, h=800, seed=0, lo=40, hi=200):
    a = np.random.default_rng(seed).random((h, w)).astype(np.float32)
    im = Image.fromarray((a * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.5))
    a = np.asarray(im).astype(float)
    a = (a - a.min()) / (a.max() - a.min()) * (hi - lo) + lo
    return Image.fromarray(a.astype(np.uint8)).convert("RGB")


def flat(v, w=600, h=400):
    return Image.fromarray(np.full((h, w, 3), v, np.uint8))


def sky_over_texture(blur=0.0, w=1200, h=800, sky=0.6):
    a = np.asarray(textured(w, h, seed=3)).copy()
    k = int(h * sky)
    a[:k] = np.linspace(150, 190, k, dtype=np.float32)[:, None, None].astype(np.uint8)  # smooth gradient sky
    im = Image.fromarray(a)
    return im.filter(ImageFilter.GaussianBlur(blur)) if blur else im


def horizon(angle, w=1200, h=800, two_lines=True):
    """Anti-aliased horizon; angle>0 descends to the right."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    t = np.tan(np.radians(angle))
    c1 = np.clip(yy - (h * 0.4 + (xx - w / 2) * t) + 0.5, 0, 1)
    v = 200 * (1 - c1) + 60 * c1
    if two_lines:
        c2 = np.clip(yy - (h * 0.55 + (xx - w / 2) * t) + 0.5, 0, 1)
        v = 200 * (1 - c1) + 60 * c1 * (1 - c2) + 120 * c2
    return Image.fromarray(np.stack([v] * 3, -1).astype(np.uint8))


def noise_image(w=1200, h=800, seed=1):
    return Image.fromarray(np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8))


def jpeg_bytes(im, q=92):
    import io
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q)
    return b.getvalue()
