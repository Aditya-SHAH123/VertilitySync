"""
Density-classified overlay rendering and per-slice band profiles.

WHAT THIS PRODUCES
    A colour overlay on the CT that classifies every lung voxel by its
    Hounsfield density band, and a profile showing how each band is
    distributed from the top of the lung to the bottom.

HOW THIS DIFFERS FROM AN ILD TEXTURE CLASSIFIER
    Published tools (CALIPER, Imbio and similar) produce a visually similar
    overlay but label each region with a radiological PATTERN - honeycombing,
    reticulation, ground-glass. Those labels come from a supervised classifier
    trained on radiologist-annotated cases, using local morphology and texture,
    not density alone. Honeycombing in particular is defined by clustered
    cystic airspaces with identifiable walls in a subpleural distribution; no
    Hounsfield threshold can establish that.

    This module therefore classifies by MEASURED DENSITY and names each class
    for its HU range. It does not name diseases or radiological patterns,
    because it has no validated model entitled to do so. Producing those
    labels would require a trained classifier evaluated against expert
    annotation - see the README note in density_regions.py.

    The colours are a legend for density, not a severity scale.
"""

import io
import base64

import numpy as np
from PIL import Image

OVERLAY_VERSION = "1.0.0"

# Ordered low -> high density. Each entry is (key, label, hu_low, hu_high, RGB).
# Colours are chosen to stay distinguishable on a greyscale CT and to remain
# separable for the most common forms of colour vision deficiency: they vary
# in lightness as well as hue, so the bands are ordered visually even if hue
# discrimination is poor.
OVERLAY_BANDS = [
    ("very_low", "Below -950 HU", None, -950.0, (40, 90, 220)),
    ("low", "-950 to -910 HU", -950.0, -910.0, (60, 170, 235)),
    ("normal_low", "-910 to -750 HU", -910.0, -750.0, (60, 200, 160)),
    ("normal_high", "-750 to -600 HU", -750.0, -600.0, (150, 210, 70)),
    ("elevated", "-600 to -400 HU", -600.0, -400.0, (245, 175, 40)),
    ("high", "Above -400 HU", -400.0, None, (235, 80, 45)),
]


def band_index_map(hu_plane, mask_plane):
    """Per-pixel band index inside the mask; -1 outside it."""
    out = np.full(hu_plane.shape, -1, dtype=np.int8)
    for i, (_k, _lab, lo, hi, _rgb) in enumerate(OVERLAY_BANDS):
        sel = mask_plane.copy()
        if lo is not None:
            sel &= (hu_plane >= lo)
        if hi is not None:
            sel &= (hu_plane < hi)
        out[sel] = i
    return out


def render_overlay_png(hu_plane, mask_plane, ww, wl, aspect_ratio=1.0, alpha=0.45):
    """Greyscale CT with the density classification composited over the lung.

    The underlying CT is windowed exactly as the plain view is, so the overlay
    sits on the identical greyscale image the clinician is already reading -
    only the lung is tinted, and the tint is blended rather than opaque so the
    anatomy stays visible underneath.
    """
    lo, hi = wl - ww / 2.0, wl + ww / 2.0
    if hi <= lo:
        hi = lo + 1.0
    grey = np.clip((hu_plane - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    rgb = np.stack([grey] * 3, axis=-1).astype(np.float32)

    idx = band_index_map(hu_plane, mask_plane)
    for i, (_k, _lab, _lo, _hi, colour) in enumerate(OVERLAY_BANDS):
        sel = idx == i
        if not sel.any():
            continue
        rgb[sel] = rgb[sel] * (1.0 - alpha) + np.array(colour, dtype=np.float32) * alpha

    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB")
    if aspect_ratio and abs(aspect_ratio - 1.0) > 0.01:
        new_h = max(1, int(round(img.height * aspect_ratio)))
        img = img.resize((img.width, new_h), Image.NEAREST)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def slice_band_composition(hu_plane, mask_plane):
    """Percentage of the lung in this slice falling in each band."""
    total = int(mask_plane.sum())
    if total == 0:
        return {"lung_pixels": 0, "bands": {k: 0.0 for k, *_ in OVERLAY_BANDS}}
    idx = band_index_map(hu_plane, mask_plane)
    return {
        "lung_pixels": total,
        "bands": {key: round(float((idx == i).sum()) / total * 100.0, 3)
                  for i, (key, *_rest) in enumerate(OVERLAY_BANDS)},
    }


def band_profile(hu_volume, mask, geometry):
    """Band composition slice by slice, from inferior to superior.

    This is the quantitative form of "where in the lung does this density
    concentrate" - the profile a reader would otherwise have to build by
    scrolling. Each entry carries the slice's world Z so the curve is plotted
    against real anatomy rather than array index.
    """
    n_slices = hu_volume.shape[0]
    entries = []
    for k in range(n_slices):
        m = mask[k]
        if not m.any():
            continue
        comp = slice_band_composition(hu_volume[k], m)
        entries.append({
            "slice_index": k,
            "z_mm": round(float(geometry.to_world(k, 0, 0)[2]), 2),
            "lung_pixels": comp["lung_pixels"],
            "bands": comp["bands"],
        })
    entries.sort(key=lambda e: e["z_mm"])

    return {
        "status": "OK",
        "overlay_version": OVERLAY_VERSION,
        "axis": "patient superior-inferior (world Z, ascending)",
        "bands": [{"key": k, "label": lab, "hu_range": [lo, hi],
                   "color": list(rgb)} for k, lab, lo, hi, rgb in OVERLAY_BANDS],
        "slices": entries,
        "note": ("Density classification by Hounsfield range. These are not radiological "
                 "patterns: naming honeycombing, reticulation, or ground-glass requires a "
                 "classifier trained and validated against expert annotation, which this "
                 "application does not have."),
        "provenance": {"source": "voxel_computation", "model_derived": False,
                       "overlay_version": OVERLAY_VERSION},
    }
