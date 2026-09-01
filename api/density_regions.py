"""
Quantitative CT densitometry and spatial region mapping.

WHAT THIS IS
    Established, published, deterministic density measurements over a
    segmented lung, plus spatial clustering so each measured region can be
    located and inspected in the source CT. These are the metrics used in
    quantitative chest CT research.

WHAT THIS IS NOT
    Not a detector, not a classifier, not AI, and not a diagnosis. A voxel
    below -950 HU is a low-attenuation voxel; calling it "emphysema" would be
    an interpretation this software is not entitled to make. Every quantity
    here is named for WHAT IT MEASURES, never for a disease. Nothing in this
    module produces a confidence score, because there is no model to produce
    one from - deterministic measurements carry quality flags instead.

    A clinician reads these numbers. The software does not read them for them.

THRESHOLD PROVENANCE
    The HU cut-points below are the conventional ones from the quantitative CT
    literature. They are reported with their source so a reader can judge
    them, and they are configurable rather than hard-coded truths:

      -950 HU   Low-attenuation area fraction (LAA%-950). The most widely
                used lung densitometry cut-point on inspiratory CT.
                Gevenois PA et al., Am J Respir Crit Care Med 1995;152:653-7.
      -910 HU   A more inclusive low-attenuation cut-point, less specific.
                Müller NL et al., Chest 1988;94:782-7.
      -856 HU   Conventionally applied to EXPIRATORY scans. This application
                cannot tell inspiratory from expiratory acquisition, so this
                value is computed but flagged as not interpretable without
                knowing the respiratory phase.
      Perc15    The 15th percentile of the lung HU histogram; less sensitive
                to scanner and reconstruction differences than LAA%.
                Stolk J et al., Eur Respir J 2007;29:1138-43.
      -600..-250 HU   High-attenuation area fraction (HAA%), used in research
                to quantify denser-than-normal parenchyma.
                Podolanczuk AJ et al., Eur Respir J 2016;48:1442-52.

    IMPORTANT: these cut-points were established on specific scanner
    protocols and reconstruction kernels. This application does not verify
    that an imported study matches those conditions, so values are
    comparable WITHIN a study far more safely than they are comparable to
    published population figures.
"""

from datetime import datetime, timezone

import numpy as np
from scipy import ndimage

DENSITOMETRY_VERSION = "1.0.0"

OK = "OK"
WARNING = "WARNING"
NOT_AVAILABLE = "NOT_AVAILABLE"

# (key, label, hu_low, hu_high, citation, caveat)
DENSITY_BANDS = [
    ("laa_950", "Low attenuation below -950 HU", None, -950.0,
     "Gevenois 1995, Am J Respir Crit Care Med 152:653-7", None),
    ("laa_910", "Low attenuation below -910 HU", None, -910.0,
     "Muller 1988, Chest 94:782-7", None),
    ("laa_856", "Low attenuation below -856 HU", None, -856.0,
     "Conventionally an expiratory-phase cut-point",
     "Respiratory phase is not recorded by this application, so this value is "
     "not interpretable without knowing whether the scan is inspiratory or expiratory."),
    ("normal_band", "Normal-range attenuation (-950 to -600 HU)", -950.0, -600.0,
     "Descriptive band, not a published index", None),
    ("haa_600_250", "High attenuation (-600 to -250 HU)", -600.0, -250.0,
     "Podolanczuk 2016, Eur Respir J 48:1442-52", None),
]

PERCENTILE_INDEX = 15          # Perc15 densitometry index

# Spatial clustering limits. A region smaller than this is noise at CT
# resolution; returning thousands of tiny clusters would be unreadable and
# would not help anyone locate anything.
MIN_REGION_VOLUME_ML = 0.5
MAX_REGIONS_RETURNED = 40


def _now():
    return datetime.now(timezone.utc).isoformat()


def _band_mask(hu_volume, lung_mask, lo, hi):
    m = lung_mask.copy()
    if lo is not None:
        m &= (hu_volume >= lo)
    if hi is not None:
        m &= (hu_volume < hi)
    return m


def band_fractions(hu_volume, lung_mask, voxel_volume_ml):
    """Fraction of the lung falling in each density band.

    Reported as both a percentage of lung volume and an absolute volume, so a
    small percentage of a large lung is not mistaken for a trivial amount of
    tissue.
    """
    total = int(lung_mask.sum())
    if total == 0:
        return {"status": NOT_AVAILABLE, "reason": "The lung mask is empty."}

    out = {}
    for key, label, lo, hi, cite, caveat in DENSITY_BANDS:
        n = int(_band_mask(hu_volume, lung_mask, lo, hi).sum())
        entry = {
            "label": label,
            "hu_range": [lo, hi],
            "percent_of_lung": round(n / total * 100.0, 3),
            "volume_ml": round(n * voxel_volume_ml, 2),
            "voxel_count": n,
            "reference": cite,
        }
        if caveat:
            entry["caveat"] = caveat
        out[key] = entry

    values = hu_volume[lung_mask]
    out["perc15_hu"] = {
        "label": f"{PERCENTILE_INDEX}th percentile lung density (Perc{PERCENTILE_INDEX})",
        "value_hu": round(float(np.percentile(values, PERCENTILE_INDEX)), 2),
        "reference": "Stolk 2007, Eur Respir J 29:1138-43",
    }
    out["status"] = OK
    return out


def _region_descriptor(comp_mask, hu_volume, geometry, voxel_volume_ml,
                        zone_of_slice, dist_mm, side_of_column):
    """Structured description of one contiguous region.

    Everything here is measured: extent, position, density. Nothing is
    inferred about cause.
    """
    idx = np.argwhere(comp_mask)
    k0, j0, i0 = idx.min(axis=0)
    k1, j1, i1 = idx.max(axis=0)
    kc, jc, ic = idx.mean(axis=0)

    sx, sy, sz = geometry.spacing_mm
    extent_mm = [round((i1 - i0 + 1) * sx, 1),
                 round((j1 - j0 + 1) * sy, 1),
                 round((k1 - k0 + 1) * sz, 1)]

    world = geometry.to_world(float(kc), float(jc), float(ic))
    vals = hu_volume[comp_mask]
    n = int(comp_mask.sum())

    zone = zone_of_slice[int(round(kc))] if zone_of_slice is not None else None
    depth = float(dist_mm[comp_mask].mean()) if dist_mm is not None else None

    # How much of the bounding box the region actually fills. This is the
    # difference between a compact blob and a diffuse network that merely
    # SPANS a large box: on a real study a 178 mL low-attenuation region had a
    # 356 mm bounding box because it threaded through the whole lung. Without
    # this ratio, "longest extent" reads like a lesion diameter, which it is
    # emphatically not.
    bbox_voxels = int((k1 - k0 + 1) * (j1 - j0 + 1) * (i1 - i0 + 1))
    fill = n / bbox_voxels if bbox_voxels else 0.0
    if fill >= 0.40:
        morphology = "compact"
    elif fill >= 0.05:
        morphology = "irregular"
    else:
        morphology = "diffuse or branching"

    return {
        "volume_ml": round(n * voxel_volume_ml, 2),
        "voxel_count": n,
        # Longest edge of the AXIS-ALIGNED BOUNDING BOX, in millimetres. It is
        # not a diameter and not a caliper measurement. Read it together with
        # bounding_box_fill_fraction.
        "bounding_box_longest_edge_mm": round(max(extent_mm), 1),
        "bounding_box_fill_fraction": round(fill, 4),
        "morphology": morphology,
        "shape_note": ("Extent describes the bounding box, not the region. A low fill fraction "
                       "means the region is spread through that box rather than filling it, so "
                       "the extent must not be read as a lesion diameter."),
        "extent_mm": {"x": extent_mm[0], "y": extent_mm[1], "z": extent_mm[2]},
        "centroid_voxel": {"i": int(round(ic)), "j": int(round(jc)), "k": int(round(kc))},
        "centroid_mm": [round(v, 1) for v in world],
        "bounding_box_voxel": {"i": [int(i0), int(i1)], "j": [int(j0), int(j1)],
                                "k": [int(k0), int(k1)]},
        "mean_hu": round(float(vals.mean()), 1),
        "median_hu": round(float(np.median(vals)), 1),
        "min_hu": round(float(vals.min()), 1),
        "zone": zone,
        "side": side_of_column(int(round(ic))),
        "mean_distance_from_pleura_mm": round(depth, 1) if depth is not None else None,
    }


def cluster_band_regions(hu_volume, lung_mask, band_key, geometry, voxel_volume_ml,
                          zone_of_slice=None, dist_mm=None, side_of_column=None,
                          min_volume_ml=MIN_REGION_VOLUME_ML,
                          max_regions=MAX_REGIONS_RETURNED):
    """Groups voxels of one density band into contiguous regions.

    Produces a locatable list so a reader can jump to each region in the
    source CT rather than being handed a single number with no way to check
    where it came from.
    """
    band = next((b for b in DENSITY_BANDS if b[0] == band_key), None)
    if band is None:
        return {"status": NOT_AVAILABLE, "reason": f"Unknown density band {band_key!r}."}
    _key, label, lo, hi, cite, caveat = band

    mask = _band_mask(hu_volume, lung_mask, lo, hi)
    if not mask.any():
        return {"status": OK, "label": label, "regions": [], "region_count": 0,
                "note": "No voxels fall in this density band."}

    labeled, n = ndimage.label(mask, structure=np.ones((3, 3, 3), dtype=bool))
    counts = np.zeros(n + 1, dtype=np.int64)
    for k in range(labeled.shape[0]):
        counts += np.bincount(labeled[k].ravel(), minlength=n + 1)
    counts[0] = 0

    min_voxels = max(1, int(round(min_volume_ml / voxel_volume_ml)))
    order = np.argsort(counts)[::-1]
    keep = [int(i) for i in order if counts[i] >= min_voxels][:max_regions]

    side_fn = side_of_column or (lambda i: None)
    regions = []
    for rank, lab in enumerate(keep, start=1):
        d = _region_descriptor(labeled == lab, hu_volume, geometry, voxel_volume_ml,
                                zone_of_slice, dist_mm, side_fn)
        d["region_id"] = f"{band_key.upper()}-{rank:03d}"
        regions.append(d)

    n_above = int((counts >= min_voxels).sum())
    result = {
        "status": OK,
        "label": label,
        "hu_range": [lo, hi],
        "reference": cite,
        "regions": regions,
        "region_count": n_above,
        "regions_returned": len(regions),
        "min_region_volume_ml": min_volume_ml,
        "total_band_volume_ml": round(float(counts.sum()) * voxel_volume_ml, 2),
    }
    if n_above > len(regions):
        result["note"] = (f"{n_above} regions met the {min_volume_ml} mL floor; the "
                          f"{len(regions)} largest are listed.")
    if caveat:
        result["caveat"] = caveat
    return result


def analyze_density_regions(hu_volume, lung_mask, geometry, voxel_volume_ml,
                             left_mask=None, right_mask=None, zones=None,
                             zone_of_slice=None, dist_mm=None,
                             cluster_bands=("laa_950", "haa_600_250")):
    """Full densitometry: whole lung, per side, per zone, plus located regions."""
    if lung_mask is None or not lung_mask.any():
        return {"status": NOT_AVAILABLE, "reason": "No lung segmentation is available."}

    cols = lung_mask.shape[2]
    if left_mask is not None and right_mask is not None:
        left_cols = np.argwhere(left_mask.any(axis=(0, 1))).ravel()
        right_cols = np.argwhere(right_mask.any(axis=(0, 1))).ravel()
        boundary = ((left_cols.min() + right_cols.max()) / 2.0
                    if left_cols.size and right_cols.size else cols / 2.0)
        left_is_higher = left_cols.mean() > right_cols.mean() if left_cols.size else True

        def side_of_column(i):
            higher = i >= boundary
            return "left" if (higher == left_is_higher) else "right"
    else:
        def side_of_column(i):
            return None

    result = {
        "status": OK,
        "densitometry_version": DENSITOMETRY_VERSION,
        "generated_at": _now(),
        "whole_lungs": band_fractions(hu_volume, lung_mask, voxel_volume_ml),
        "interpretation_note": (
            "These are density measurements, not findings. A density band is named for the "
            "Hounsfield range it counts, never for a disease. Published cut-points were "
            "derived on specific scanner protocols that this application does not verify, "
            "so values compare far more safely within one study than against population "
            "reference figures."),
        "provenance": {
            "source": "voxel_computation",
            "method": "Direct Hounsfield thresholding of the segmented lung, with 26-connectivity "
                      "spatial clustering of each band.",
            "model_derived": False,
            "densitometry_version": DENSITOMETRY_VERSION,
        },
    }

    if left_mask is not None and right_mask is not None:
        result["left_lung"] = band_fractions(hu_volume, left_mask, voxel_volume_ml)
        result["right_lung"] = band_fractions(hu_volume, right_mask, voxel_volume_ml)

    if zones:
        result["zones"] = {name: band_fractions(hu_volume, zmask, voxel_volume_ml)
                           for name, zmask in zones.items()}

    result["regions"] = {}
    for band_key in cluster_bands:
        result["regions"][band_key] = cluster_band_regions(
            hu_volume, lung_mask, band_key, geometry, voxel_volume_ml,
            zone_of_slice=zone_of_slice, dist_mm=dist_mm, side_of_column=side_of_column)

    return result
