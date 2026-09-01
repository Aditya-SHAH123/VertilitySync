"""
Lung segmentation module.

SEGMENTATION METHOD (read this before trusting or extending the output):
    Classical, rule-based 3D image processing - NOT a trained machine-learning
    or AI model. Specifically:
        1. Body-mask isolation via border-connected external-air removal
           + per-slice hole filling.
        2. Hounsfield-unit thresholding to find air-density candidate regions
           inside the body silhouette.
        3. 3D connected-component analysis (26-connectivity) to isolate
           discrete air-filled regions.
        4. Physical-volume-based filtering of small/noise components
           (e.g. bowel gas, imaging artifacts) and internal hole filling
           within kept components (to close vessel/airway holes for a
           solid, mesh-able surface).
        5. Left/right assignment of the two largest qualifying components
           using the DICOM patient (LPS) coordinate frame, when reliable
           image orientation is available for the study.

This module never fabricates a result: if the input data is insufficient
(no calibrated Hounsfield units, empty candidate mask) or the result is not
anatomically plausible (implausibly large/small), it returns a FAIL status
and the caller must not proceed to 3D reconstruction.

No data leaves this process. No external AI/ML API is called here.

Future-compatibility note: this module is intentionally isolated behind the
`segment_lungs(...) -> SegmentationResult` interface so a trained model could
be substituted later (see NEXT step: `method` / `method_version` fields exist
specifically so any future model-based implementation is clearly labeled and
distinguishable from this rule-based baseline in every API response).
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import ndimage

# ---------------------------------------------------------------------------
# Method identity - surfaced verbatim in API responses. Do not call this "AI".
# ---------------------------------------------------------------------------
SEGMENTATION_METHOD = (
    "Rule-based image processing: HU thresholding + body-mask isolation + "
    "3D connected-component analysis + morphological cleanup. "
    "This is NOT a trained AI/ML model."
)
SEGMENTATION_METHOD_VERSION = "1.0.0-ruleset"

# ---------------------------------------------------------------------------
# Tunable constants. These are physically-motivated HU/volume thresholds,
# not fitted to any single scan.
# ---------------------------------------------------------------------------
EXTERNAL_AIR_HU = -900.0          # near-true-air HU used to find scanner background air
LUNG_CANDIDATE_HU_MAX = -300.0    # air/lung-parenchyma-density ceiling inside the body
MIN_COMPONENT_VOLUME_ML = 5.0     # components smaller than this are treated as noise
MIN_PLAUSIBLE_LUNG_VOLUME_ML = 200.0   # below this, segmentation is considered a failure
# Skin/air partial-volume voxels sit between the external-air threshold and the
# lung-candidate ceiling, forming a thin shell around the body that otherwise
# bridges the two lungs to each other (and to outside air) around the chest
# wall. Eroding the body silhouette by a physical distance removes it. Measured
# on a real 255-slice chest CT, this was the difference between a 6,012 mL
# single blob spanning the full field of view and a correct 4,968 mL lung mask.
BODY_EROSION_MM = 3.0
# Soft tissue floor used to find the patient's body. The scanner table is a
# separate connected component beyond the patient-table air gap, so taking the
# largest tissue component excludes it - and with it the gap, which would
# otherwise be counted as lung. See the comment in segment_lungs().
BODY_TISSUE_HU_MIN = -300.0
# A genuine second lung is comparable in volume to the first. Below this ratio
# the runner-up component is treated as something other than lung, and the
# largest component is assumed to contain both lungs joined at the airway.
SECOND_LUNG_MIN_RATIO = 0.30
# Two components are accepted as left+right lungs only if their column
# centroids are separated by at least this fraction of their combined extent.
# Size similarity alone let two non-lateralised regions be labelled as lungs
# on a real study; see the check in segment_lungs().
LATERAL_SEPARATION_MIN = 0.25
MAX_PLAUSIBLE_LUNG_FRACTION = 0.60     # lungs occupying >60% of the body silhouette is implausible
REQUIRED_HU_AVAILABILITY_FRACTION = 0.90  # below this fraction of slices with real HU, refuse to segment


@dataclass
class SegmentationResult:
    success: bool
    status: str                       # 'OK' | 'WARNING' | 'FAIL'
    method: str
    method_version: str
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    mask: Optional[np.ndarray] = None        # bool [slices, rows, cols] - combined (both lungs)
    left_mask: Optional[np.ndarray] = None   # bool, same shape, or None if not determinable
    right_mask: Optional[np.ndarray] = None  # bool, same shape, or None if not determinable

    def to_public_dict(self):
        """JSON-safe summary (never includes the raw mask arrays)."""
        return {
            "success": self.success,
            "status": self.status,
            "method": self.method,
            "method_version": self.method_version,
            "warnings": self.warnings,
            "stats": self.stats,
            "left_right_available": self.left_mask is not None and self.right_mask is not None,
        }


def _voxel_volume_mm3(spacing_mm):
    sx, sy, sz = spacing_mm
    return float(sx) * float(sy) * float(sz)


def _component_sizes(labeled, n_components):
    """Voxel count per label, accumulated one slice at a time.

    Memory matters here: on a real chest CT the labeled array is int32 and the
    same size as the volume (hundreds of MB). The obvious formulations both
    allocate another full-volume temporary -
        ndimage.sum(np.ones_like(labeled), labeled, ...)   ~5x volume peak
        np.bincount(labeled.ravel())                       ~2x volume peak
    - whereas accumulating per slice needs only one small counter array.
    Measured on a 512x512x120 volume: 629 MB -> 252 MB -> 2 MB, identical result.
    """
    counts = np.zeros(n_components + 1, dtype=np.int64)
    for k in range(labeled.shape[0]):
        counts += np.bincount(labeled[k].ravel(), minlength=n_components + 1)
    return counts


def _labels_to_mask(labeled, label_values, n_components):
    """Boolean mask of the given labels via a lookup table.

    Preferred over np.isin, which sorts and allocates additional full-volume
    temporaries; the LUT indexing allocates only the boolean output.
    """
    lut = np.zeros(n_components + 1, dtype=bool)
    valid = [int(v) for v in label_values if 0 < int(v) <= n_components]
    if valid:
        lut[valid] = True
    return lut[labeled]


def _largest_slice_axis_border_touch(mask):
    """Whether the mask touches the in-plane (row/column) image border.

    Touching the slice-axis (top/bottom of the scan range) is common and
    expected for a chest CT that starts or ends within the lung field, so it
    is reported separately and does not by itself count as a warning.
    """
    return bool(
        mask[:, 0, :].any() or mask[:, -1, :].any()
        or mask[:, :, 0].any() or mask[:, :, -1].any()
    )


def segment_lungs(hu_volume, hu_available_per_slice, spacing_mm, col_cosines=None,
                   orientation_reliable=False):
    """
    Segment the lungs from a reconstructed HU volume.

    Args:
        hu_volume: float32 ndarray [slices, rows, cols], Hounsfield units.
        hu_available_per_slice: list[bool], per-slice HU conversion validity.
        spacing_mm: (spacing_x, spacing_y, spacing_z) mm per voxel, i.e.
            (column spacing, row spacing, slice spacing) - see mesh_reconstruction
            for the full coordinate documentation.
        col_cosines: unit vector, direction of increasing column index in the
            DICOM patient (LPS) frame; used only to assign left/right labels.
        orientation_reliable: whether col_cosines came from real, non-fallback
            DICOM geometry. If False, left/right assignment is skipped rather
            than guessed.

    Returns:
        SegmentationResult
    """
    warnings = []
    stats = {}

    n_slices = hu_volume.shape[0]
    hu_fraction_available = (sum(1 for v in hu_available_per_slice if v) / n_slices) if n_slices else 0.0
    stats["hu_availability_fraction"] = round(hu_fraction_available, 4)

    if hu_fraction_available < REQUIRED_HU_AVAILABILITY_FRACTION:
        return SegmentationResult(
            success=False, status="FAIL",
            method=SEGMENTATION_METHOD, method_version=SEGMENTATION_METHOD_VERSION,
            warnings=[
                f"Calibrated Hounsfield units are available for only "
                f"{hu_fraction_available * 100:.0f}% of slices; HU-threshold segmentation "
                f"requires reliable HU data (>= {REQUIRED_HU_AVAILABILITY_FRACTION * 100:.0f}%) "
                f"and was refused rather than run on uncalibrated pixel values."
            ],
            stats=stats,
        )
    if hu_fraction_available < 1.0:
        warnings.append(
            f"HU conversion was unavailable for {round((1 - hu_fraction_available) * 100)}% of "
            f"slices; segmentation proceeded but is less reliable on those slices."
        )

    voxel_vol_mm3 = _voxel_volume_mm3(spacing_mm)
    voxel_vol_ml = voxel_vol_mm3 / 1000.0

    # --- 1. External (scanner background) air: HU-low regions touching the volume border ---
    air_like = hu_volume <= EXTERNAL_AIR_HU
    border_structure = np.ones((3, 3, 3), dtype=bool)
    labeled_air, n_air_components = ndimage.label(air_like, structure=border_structure)

    if n_air_components > 0:
        border_labels = set()
        border_labels.update(np.unique(labeled_air[0, :, :]))
        border_labels.update(np.unique(labeled_air[-1, :, :]))
        border_labels.update(np.unique(labeled_air[:, 0, :]))
        border_labels.update(np.unique(labeled_air[:, -1, :]))
        border_labels.update(np.unique(labeled_air[:, :, 0]))
        border_labels.update(np.unique(labeled_air[:, :, -1]))
        border_labels.discard(0)
        external_air_mask = (_labels_to_mask(labeled_air, border_labels, n_air_components)
                              if border_labels else np.zeros_like(air_like))
    else:
        external_air_mask = np.zeros_like(air_like)

    # labeled_air is int32 and volume-sized (hundreds of MB on a real study);
    # it is dead from here on, so release it before the next labeling pass
    # rather than letting two full-volume label arrays coexist.
    del labeled_air, air_like

    if not external_air_mask.any():
        warnings.append(
            "No scanner background air was detected touching the volume border "
            "(the field of view may be tightly cropped to the body); the body "
            "silhouette could not be refined and plausibility checks below are "
            "relied on more heavily."
        )

    # --- 2. Body silhouette: the patient, excluding the scanner table ---
    # Inverting external air and filling holes is not enough. The air gap
    # between the patient's back and the table is enclosed - bounded by the
    # table below and the body above - so it is neither external air nor a
    # hole inside the body, and it survives as a large slab of lung-density
    # voxels. On a real study that slab contributed 5,062 px on a single
    # slice at a mean of -831 HU, inflating lung volume and appearing as a
    # spurious second "lung" component.
    #
    # Deriving the silhouette from tissue instead fixes it: the patient and
    # the table are separate connected components of soft tissue (the air gap
    # separates them), so keeping only the largest drops the table, and the
    # gap then falls outside the body entirely.
    tissue = hu_volume > BODY_TISSUE_HU_MIN
    body_silhouette = None
    if tissue.any():
        tissue_labels, n_tissue = ndimage.label(tissue, structure=border_structure)
        if n_tissue >= 1:
            tissue_counts = _component_sizes(tissue_labels, n_tissue)
            tissue_counts[0] = 0
            largest_tissue = int(np.argmax(tissue_counts))
            patient_body = (tissue_labels == largest_tissue)
            stats["tissue_components_excluded"] = int(n_tissue - 1)
            body_silhouette = np.empty_like(patient_body)
            for k in range(n_slices):
                body_silhouette[k] = ndimage.binary_fill_holes(patient_body[k])
            del tissue_labels, patient_body
    del tissue

    if body_silhouette is None:
        # No soft tissue at all: fall back to the inverted-air silhouette so a
        # phantom or an unusual study still gets a result rather than an error.
        warnings.append("No soft-tissue component was found; the body outline was derived from "
                        "air inversion instead, which cannot exclude the scanner table.")
        body_silhouette = np.empty_like(external_air_mask, dtype=bool)
        for k in range(n_slices):
            body_silhouette[k] = ndimage.binary_fill_holes(~external_air_mask[k])

    stats["body_volume_ml"] = round(float(body_silhouette.sum()) * voxel_vol_ml, 1)

    if body_silhouette.sum() == 0:
        return SegmentationResult(
            success=False, status="FAIL",
            method=SEGMENTATION_METHOD, method_version=SEGMENTATION_METHOD_VERSION,
            warnings=warnings + ["Body silhouette isolation produced an empty mask; cannot proceed."],
            stats=stats,
        )

    # --- 3. Lung-density candidates within the body ---
    # Erode the silhouette first (in-plane only - slice spacing is usually
    # coarser than pixel spacing) to strip the skin-surface partial-volume
    # shell described at BODY_EROSION_MM. The erosion is expressed in
    # millimetres so it behaves the same at any pixel spacing.
    erosion_px = max(1, int(round(BODY_EROSION_MM / max(float(spacing_mm[0]), 1e-6))))
    body_interior = ndimage.binary_erosion(
        body_silhouette, structure=np.ones((1, 3, 3), dtype=bool), iterations=erosion_px)
    stats["body_erosion_px"] = erosion_px

    lung_candidate = (hu_volume <= LUNG_CANDIDATE_HU_MAX) & body_interior

    if not lung_candidate.any():
        return SegmentationResult(
            success=False, status="FAIL",
            method=SEGMENTATION_METHOD, method_version=SEGMENTATION_METHOD_VERSION,
            warnings=warnings + ["No air/lung-density voxels were found inside the body silhouette."],
            stats=stats,
        )

    # --- 4. Connected components + physical-volume-based noise filtering ---
    full_structure = np.ones((3, 3, 3), dtype=bool)
    labeled, n_components = ndimage.label(lung_candidate, structure=full_structure)
    stats["raw_component_count"] = int(n_components)

    # Both are volume-sized and dead once `labeled` exists; the body volume
    # stat was already recorded above.
    del lung_candidate, body_silhouette, external_air_mask

    counts = _component_sizes(labeled, n_components) if n_components else np.zeros(1, dtype=np.int64)

    # Any component reaching the left/right/anterior/posterior edge of the
    # field of view is outside air, the gap between the patient and the
    # scanner table, or something else that is not lung - real lungs never
    # touch the in-plane border. Excluding them here is what removes the
    # table air-gap that survives the body mask.
    border_labels = set()
    for face in (labeled[:, 0, :], labeled[:, -1, :], labeled[:, :, 0], labeled[:, :, -1]):
        border_labels.update(int(v) for v in np.unique(face))
    border_labels.discard(0)
    stats["border_touching_components_excluded"] = len(border_labels)

    component_info = []
    for idx in range(1, n_components + 1):
        if idx in border_labels:
            continue
        size = int(counts[idx])
        vol_ml = float(size) * voxel_vol_ml
        if vol_ml >= MIN_COMPONENT_VOLUME_ML:
            component_info.append((idx, size, vol_ml))
    component_info.sort(key=lambda t: -t[1])

    dropped = n_components - len(component_info)
    if dropped > 0:
        warnings.append(
            f"{dropped} small candidate region(s) below {MIN_COMPONENT_VOLUME_ML:.0f} mL "
            f"were excluded as noise (e.g. bowel gas, artifacts)."
        )
    stats["kept_component_count"] = len(component_info)

    if not component_info:
        return SegmentationResult(
            success=False, status="FAIL",
            method=SEGMENTATION_METHOD, method_version=SEGMENTATION_METHOD_VERSION,
            warnings=warnings + ["No candidate region met the minimum plausible lung-component volume."],
            stats=stats,
        )

    # Two separately-labelled lungs are always roughly comparable in size. A
    # much smaller runner-up is something else (stomach gas, a detached airway
    # branch), so keeping the "two largest" unconditionally would label a
    # 236 mL bubble as a whole lung - observed on a real study where the true
    # lungs were a single 4,960 mL component joined at the airway.
    if len(component_info) >= 2 and component_info[1][2] >= SECOND_LUNG_MIN_RATIO * component_info[0][2]:
        top = component_info[:2]
    else:
        top = component_info[:1]
    excluded_significant = len(component_info) - len(top)
    if excluded_significant > 0:
        warnings.append(
            f"{excluded_significant} additional air-filled region(s) above the noise threshold "
            f"(e.g. trachea/main airway) were excluded from the lung mask, which is limited to "
            f"the two largest regions."
        )

    combined_mask = _labels_to_mask(labeled, [t[0] for t in top], n_components)
    # Close small internal holes (vessels/airway lumens) for a solid, mesh-able surface.
    combined_mask = ndimage.binary_fill_holes(combined_mask)

    lung_volume_ml = float(combined_mask.sum()) * voxel_vol_ml
    lung_fraction_of_body = lung_volume_ml / stats["body_volume_ml"] if stats["body_volume_ml"] else 0.0
    stats["lung_volume_ml"] = round(lung_volume_ml, 1)
    stats["lung_fraction_of_body"] = round(lung_fraction_of_body, 4)

    inplane_border_touch = _largest_slice_axis_border_touch(combined_mask)
    stats["touches_inplane_border"] = inplane_border_touch
    stats["touches_scan_range_start_or_end"] = bool(combined_mask[0].any() or combined_mask[-1].any())
    if inplane_border_touch:
        warnings.append(
            "The segmented region touches the left/right/anterior/posterior edge of the "
            "reconstructed volume, which is anatomically unusual and may indicate the field "
            "of view clips the thorax or that non-lung structures were included."
        )

    # --- 5. Left / right assignment ---
    left_mask = None
    right_mask = None
    # Two comparable components are only left and right lungs if they actually
    # sit on OPPOSITE SIDES. Size similarity alone is not enough: on a real
    # study, two components of 1,051 mL and 3,246 mL passed the size test while
    # both spanned the whole chest with near-identical column centroids (254
    # and 249) - they were not lungs at all, and the resulting "left/right"
    # volumes were meaningless. Verify lateral separation before trusting them.
    lateralised = False
    if len(top) == 2:
        idx_a, idx_b = top[0][0], top[1][0]
        mask_a, mask_b = (labeled == idx_a), (labeled == idx_b)
        cols_a = np.argwhere(mask_a.any(axis=(0, 1))).ravel()
        cols_b = np.argwhere(mask_b.any(axis=(0, 1))).ravel()
        if cols_a.size and cols_b.size:
            centroid_a, centroid_b = float(cols_a.mean()), float(cols_b.mean())
            extent = float(max(cols_a.max(), cols_b.max()) - min(cols_a.min(), cols_b.min()))
            separation = abs(centroid_a - centroid_b) / extent if extent > 0 else 0.0
            # Neither true lung crosses the midline of the pair.
            midpoint = (centroid_a + centroid_b) / 2.0
            a_side = (cols_a.min() >= midpoint) or (cols_a.max() <= midpoint)
            b_side = (cols_b.min() >= midpoint) or (cols_b.max() <= midpoint)
            lateralised = separation >= LATERAL_SEPARATION_MIN and a_side and b_side
            stats["component_lateral_separation"] = round(separation, 3)
        if not lateralised:
            warnings.append(
                "The two largest air-filled regions are not laterally separated, so they are not "
                "left and right lungs. They were merged and divided at the sagittal midline instead."
            )
            # Fold the pair back into one region and take the midline path below.
            top = [top[0]]
            combined_mask = _labels_to_mask(labeled, [idx_a, idx_b], n_components)
            combined_mask = ndimage.binary_fill_holes(combined_mask)
            lung_volume_ml = float(combined_mask.sum()) * voxel_vol_ml
            lung_fraction_of_body = (lung_volume_ml / stats["body_volume_ml"]
                                      if stats["body_volume_ml"] else 0.0)
            stats["lung_volume_ml"] = round(lung_volume_ml, 1)
            stats["lung_fraction_of_body"] = round(lung_fraction_of_body, 4)

    if lateralised and orientation_reliable and col_cosines is not None:
        idx_a, idx_b = top[0][0], top[1][0]
        centroid_a = ndimage.center_of_mass(labeled == idx_a)[2]  # column index
        centroid_b = ndimage.center_of_mass(labeled == idx_b)[2]
        x_sign = 1.0 if np.dot(col_cosines, (1.0, 0.0, 0.0)) >= 0 else -1.0
        # DICOM patient (LPS) frame: +X = patient LEFT. Larger projected-X centroid = left lung.
        a_is_left = (centroid_a * x_sign) > (centroid_b * x_sign)
        left_idx = idx_a if a_is_left else idx_b
        right_idx = idx_b if a_is_left else idx_a
        left_mask = (labeled == left_idx)
        right_mask = (labeled == right_idx)
        left_vol_ml = float(left_mask.sum()) * voxel_vol_ml
        right_vol_ml = float(right_mask.sum()) * voxel_vol_ml
        balance = min(left_vol_ml, right_vol_ml) / max(left_vol_ml, right_vol_ml) if max(left_vol_ml, right_vol_ml) else 0
        stats["left_lung_volume_ml"] = round(left_vol_ml, 1)
        stats["right_lung_volume_ml"] = round(right_vol_ml, 1)
        stats["left_right_balance"] = round(balance, 3)
        if balance < 0.15:
            warnings.append(
                "Left/right lung volumes are highly imbalanced; this may reflect real pathology, "
                "an incomplete scan, or a segmentation error - inspect both the 3D model and the "
                "original CT slices before drawing conclusions."
            )
    elif len(top) == 1 and orientation_reliable and col_cosines is not None:
        # On a real study the two lungs are usually a SINGLE connected
        # component, joined through the trachea and main bronchi, whose lumen
        # sits inside the lung-candidate HU range. Splitting at the sagittal
        # midline is anatomically valid because neither lung crosses it. The
        # midline is taken as the narrowest point of the per-column profile -
        # the mediastinum - rather than the mask centroid, which is pulled off
        # centre when one lung is larger.
        col_profile = combined_mask.sum(axis=(0, 1))
        occupied = np.argwhere(col_profile > 0).ravel()
        if occupied.size > 8:
            lo, hi = int(occupied.min()), int(occupied.max())
            inner = col_profile[lo:hi + 1]
            q = max(len(inner) // 4, 1)
            split = lo + q + int(np.argmin(inner[q:len(inner) - q])) if len(inner) > 2 * q else lo + len(inner) // 2

            x_sign = 1.0 if np.dot(col_cosines, (1.0, 0.0, 0.0)) >= 0 else -1.0
            higher = combined_mask.copy(); higher[:, :, :split] = False
            lower = combined_mask.copy(); lower[:, :, split:] = False
            # DICOM patient (LPS): +X is patient LEFT.
            left_mask, right_mask = (higher, lower) if x_sign > 0 else (lower, higher)

            left_vol_ml = float(left_mask.sum()) * voxel_vol_ml
            right_vol_ml = float(right_mask.sum()) * voxel_vol_ml
            balance = (min(left_vol_ml, right_vol_ml) / max(left_vol_ml, right_vol_ml)
                       if max(left_vol_ml, right_vol_ml) else 0)
            stats["left_lung_volume_ml"] = round(left_vol_ml, 1)
            stats["right_lung_volume_ml"] = round(right_vol_ml, 1)
            stats["left_right_balance"] = round(balance, 3)
            stats["left_right_method"] = "sagittal_midline_split"
            warnings.append(
                "The lungs form a single connected region (joined through the airway), so "
                "left and right were separated at the sagittal midline rather than as "
                "distinct components. The division near the mediastinum is approximate."
            )
            if balance < 0.15:
                warnings.append(
                    "Left/right lung volumes are highly imbalanced; this may reflect real pathology, "
                    "an incomplete scan, or a segmentation error - inspect both the 3D model and the "
                    "original CT slices before drawing conclusions."
                )
        else:
            warnings.append("The segmented region is too narrow to split into left and right lungs.")
    elif len(top) == 2:
        warnings.append(
            "Left/right lung labeling was skipped: reliable DICOM image orientation is not "
            "available for this study (fallback slice ordering was used)."
        )
    else:
        warnings.append(
            "Only one major air-filled region met the lung-volume threshold; left/right "
            "separation is not applicable."
        )

    # --- 6. Plausibility gate ---
    if lung_volume_ml < MIN_PLAUSIBLE_LUNG_VOLUME_ML:
        return SegmentationResult(
            success=False, status="FAIL",
            method=SEGMENTATION_METHOD, method_version=SEGMENTATION_METHOD_VERSION,
            warnings=warnings + [
                f"Segmented volume ({lung_volume_ml:.0f} mL) is implausibly small for lung "
                f"parenchyma (< {MIN_PLAUSIBLE_LUNG_VOLUME_ML:.0f} mL floor)."
            ],
            stats=stats, mask=combined_mask, left_mask=left_mask, right_mask=right_mask,
        )
    if lung_fraction_of_body > MAX_PLAUSIBLE_LUNG_FRACTION:
        return SegmentationResult(
            success=False, status="FAIL",
            method=SEGMENTATION_METHOD, method_version=SEGMENTATION_METHOD_VERSION,
            warnings=warnings + [
                f"Segmented volume is {lung_fraction_of_body * 100:.0f}% of the body silhouette, "
                f"above the {MAX_PLAUSIBLE_LUNG_FRACTION * 100:.0f}% plausibility ceiling."
            ],
            stats=stats, mask=combined_mask, left_mask=left_mask, right_mask=right_mask,
        )

    status = "WARNING" if warnings else "OK"
    return SegmentationResult(
        success=True, status=status,
        method=SEGMENTATION_METHOD, method_version=SEGMENTATION_METHOD_VERSION,
        warnings=warnings, stats=stats,
        mask=combined_mask, left_mask=left_mask, right_mask=right_mask,
    )
