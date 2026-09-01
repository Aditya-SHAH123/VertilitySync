"""
Unit tests for api/lung_segmentation.py using purely synthetic, in-memory HU
volumes (no DICOM I/O, no real patient data anywhere).
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from lung_segmentation import segment_lungs  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'PASS: {name}')
    else:
        FAIL.append(name)
        print(f'FAIL: {name} {extra}')


def build_phantom_volume(n_slices=30, rows=100, cols=100, spacing=(1.4, 1.4, 4.0),
                          lung_slice_range=(3, 27), body_radius_mm=55.0,
                          lung_radius_mm=20.0, lung_offset_mm=28.0):
    sx, sy, sz = spacing
    cx, cy = cols / 2.0, rows / 2.0
    yy, xx = np.mgrid[0:rows, 0:cols]
    dx_mm = (xx - cx) * sx
    dy_mm = (yy - cy) * sy
    body = (dx_mm ** 2 + dy_mm ** 2) <= body_radius_mm ** 2
    left_lung = ((dx_mm - lung_offset_mm) ** 2 + dy_mm ** 2) <= lung_radius_mm ** 2
    right_lung = ((dx_mm + lung_offset_mm) ** 2 + dy_mm ** 2) <= lung_radius_mm ** 2

    volume = np.full((n_slices, rows, cols), -1000.0, dtype=np.float32)
    lo, hi = lung_slice_range
    for k in range(n_slices):
        volume[k][body] = 40.0
        if lo <= k < hi:
            volume[k][left_lung] = -800.0
            volume[k][right_lung] = -800.0
    return volume


def build_realistic_phantom(n_slices=50, rows=160, cols=160, spacing=(1.0, 1.0, 2.5)):
    """Phantom reproducing the three failure modes found on a real chest CT.

    The earlier phantom is clean geometry and passed while the segmentation was
    badly wrong on real data. This one adds the features that actually broke it:

      1. A skin/air partial-volume shell (~-600 HU) around the body. It sits
         between the external-air and lung-candidate thresholds, so it joins
         both lungs to each other and to outside air around the chest wall.
      2. A scanner table with an enclosed air gap beneath the patient, which
         per-slice hole filling wrongly treats as inside the body.
      3. A trachea joining the two lungs, so they form ONE connected component
         rather than two - the normal case on a real study.
      4. A small gas bubble, which must not be mistaken for the second lung.
    """
    sx, sy, sz = spacing
    cx, cy = cols / 2.0, rows / 2.0
    yy, xx = np.mgrid[0:rows, 0:cols]
    dx, dy = (xx - cx) * sx, (yy - cy) * sy

    body_r, shell_r = 58.0, 61.0
    lung_r, lung_off = 24.0, 26.0

    body = (dx ** 2 + dy ** 2) <= body_r ** 2
    shell = ((dx ** 2 + dy ** 2) <= shell_r ** 2) & ~body
    lungL = ((dx - lung_off) ** 2 + dy ** 2) <= lung_r ** 2
    lungR = ((dx + lung_off) ** 2 + dy ** 2) <= lung_r ** 2
    trachea = (np.abs(dx) <= 3.0) & (np.abs(dy) <= 3.0)
    bubble = ((dx - 30.0) ** 2 + (dy - 34.0) ** 2) <= 6.0 ** 2

    vol = np.full((n_slices, rows, cols), -1000.0, dtype=np.float32)
    table_row = rows - 6
    for k in range(n_slices):
        vol[k][body] = 40.0
        vol[k][shell] = -600.0                 # (1) partial-volume skin shell
        vol[k][table_row:, :] = 300.0          # (2) table, with air gap above it
        if 4 <= k < n_slices - 4:
            vol[k][lungL] = -800.0
            vol[k][lungR] = -800.0
            vol[k][trachea] = -900.0           # (3) joins the two lungs
        if 8 <= k < 14:
            vol[k][bubble] = -850.0            # (4) small gas bubble
    return vol


def main():
    spacing = (1.4, 1.4, 4.0)

    # --- happy path: symmetric two-lung phantom ---
    volume = build_phantom_volume(spacing=spacing)
    hu_available = [True] * volume.shape[0]
    result = segment_lungs(volume, hu_available, spacing, col_cosines=(1.0, 0.0, 0.0), orientation_reliable=True)

    check('phantom segmentation succeeds', result.success, result.warnings)
    check('phantom segmentation status OK or WARNING', result.status in ('OK', 'WARNING'), result.status)
    check('phantom mask is non-empty', result.mask is not None and result.mask.sum() > 0)
    check('phantom left/right both identified', result.left_mask is not None and result.right_mask is not None)
    if result.left_mask is not None and result.right_mask is not None:
        left_col_centroid = np.argwhere(result.left_mask.any(axis=0))[:, 1].mean()
        right_col_centroid = np.argwhere(result.right_mask.any(axis=0))[:, 1].mean()
        check('left lung is at the larger column index (patient +X = Left, per LPS)',
              left_col_centroid > right_col_centroid, (left_col_centroid, right_col_centroid))
    check('lung volume stat is plausible (100-400 mL for this phantom)',
          result.stats.get('lung_volume_ml', 0) > 100, result.stats.get('lung_volume_ml'))
    check('left/right balance near 1.0 for symmetric phantom',
          abs(result.stats.get('left_right_balance', 0) - 1.0) < 0.05, result.stats.get('left_right_balance'))

    # --- empty candidate mask: uniform soft tissue, no air-density regions ---
    uniform = np.full((10, 40, 40), 40.0, dtype=np.float32)
    empty_result = segment_lungs(uniform, [True] * 10, spacing, orientation_reliable=False)
    check('uniform soft-tissue volume is rejected (no candidates)', not empty_result.success, empty_result.status)
    check('uniform soft-tissue volume status is FAIL', empty_result.status == 'FAIL')

    # --- implausibly large "lungs" (whole body is low-HU) ---
    all_air_body = build_phantom_volume(spacing=spacing)
    all_air_body[all_air_body == 40.0] = -800.0  # replace all soft tissue with lung-density HU
    implausible = segment_lungs(all_air_body, [True] * all_air_body.shape[0], spacing,
                                 col_cosines=(1.0, 0.0, 0.0), orientation_reliable=True)
    check('implausibly large lung region is rejected', not implausible.success, implausible.status)

    # --- insufficient HU availability ---
    volume2 = build_phantom_volume(spacing=spacing)
    sparse_hu = [False] * volume2.shape[0]
    sparse_hu[0] = True
    no_hu_result = segment_lungs(volume2, sparse_hu, spacing, orientation_reliable=True)
    check('segmentation refuses to run without sufficient HU data', not no_hu_result.success)
    check('HU-insufficiency reason is stated', 'Hounsfield' in no_hu_result.warnings[0])

    # --- left/right skipped without reliable orientation ---
    volume3 = build_phantom_volume(spacing=spacing)
    no_orientation_result = segment_lungs(volume3, [True] * volume3.shape[0], spacing, orientation_reliable=False)
    check('left/right skipped when orientation is unreliable',
          no_orientation_result.left_mask is None and no_orientation_result.right_mask is None)
    check('mask still produced when orientation is unreliable (spacing-only reconstruction still valid)',
          no_orientation_result.success and no_orientation_result.mask.sum() > 0)

    # --- realistic phantom: the failure modes found on a real chest CT ---
    rspacing = (1.0, 1.0, 2.5)
    rvol = build_realistic_phantom(spacing=rspacing)
    r = segment_lungs(rvol, [True] * rvol.shape[0], rspacing,
                       col_cosines=(1.0, 0.0, 0.0), orientation_reliable=True)

    check('realistic phantom segments successfully', r.success, r.warnings)
    check('body erosion was applied', r.stats.get('body_erosion_px', 0) >= 1,
          r.stats.get('body_erosion_px'))

    # (1)+(2) the mask must not leak out to the image edge via the skin shell
    #         or the table air-gap
    check('mask does not touch the in-plane border',
          r.stats.get('touches_inplane_border') is False, r.stats.get('touches_inplane_border'))

    # REGRESSION: the patient-table air gap must not be counted as lung. It is
    # enclosed (table below, body above), so it is neither external air nor a
    # hole inside the body, and border rejection cannot see it - the table sits
    # between the gap and the image edge. On a real study it contributed 5,062
    # px on one slice at a mean of -831 HU and appeared as a spurious lung.
    table_row_start = rvol.shape[1] - 6
    gap_zone = r.mask[:, table_row_start - 12:table_row_start, :]
    check('the patient-table air gap is excluded from the lung mask',
          int(gap_zone.sum()) == 0, int(gap_zone.sum()))
    check('the table itself is excluded from the body silhouette',
          r.stats.get('tissue_components_excluded', 0) >= 1,
          r.stats.get('tissue_components_excluded'))
    mask_rows = np.argwhere(r.mask.any(axis=(0, 2))).ravel()
    check('the lung mask stays above the table row',
          mask_rows.max() < table_row_start, (int(mask_rows.max()), table_row_start))
    cols_used = np.argwhere(r.mask.any(axis=(0, 1))).ravel()
    check('mask does not span the full field of view',
          cols_used.min() > 5 and cols_used.max() < rvol.shape[2] - 6,
          (int(cols_used.min()), int(cols_used.max())))

    # (3) lungs joined by the trachea are one component, split at the midline
    check('merged lungs are split into left and right',
          r.left_mask is not None and r.right_mask is not None)
    check('midline split method is reported',
          r.stats.get('left_right_method') == 'sagittal_midline_split',
          r.stats.get('left_right_method'))
    check('the midline split is disclosed as approximate',
          any('sagittal midline' in w for w in r.warnings), r.warnings)
    check('left/right volumes are balanced for a symmetric phantom',
          r.stats.get('left_right_balance', 0) > 0.80, r.stats.get('left_right_balance'))

    # (4) a small gas bubble must not be promoted to "the other lung"
    lung_ml = r.stats.get('lung_volume_ml', 0)
    check('small gas bubble is not counted as a lung',
          abs(r.stats.get('left_lung_volume_ml', 0) - r.stats.get('right_lung_volume_ml', 0))
          < 0.5 * lung_ml, (r.stats.get('left_lung_volume_ml'), r.stats.get('right_lung_volume_ml')))

    # a genuinely bilateral, separately-labelled pair must still take the
    # two-component path rather than the midline split
    check('separate bilateral lungs still use component labelling',
          result.stats.get('left_right_method') is None, result.stats.get('left_right_method'))
    check('a properly lateralised pair reports high separation',
          result.stats.get('component_lateral_separation', 0) > 0.25,
          result.stats.get('component_lateral_separation'))

    # REGRESSION: two components of comparable size that are NOT on opposite
    # sides must not be labelled left and right. On a real study, components of
    # 1,051 mL and 3,246 mL passed the size test while both spanned the whole
    # chest (column centroids 254 and 249), producing meaningless per-side
    # volumes. Here the two regions are stacked front-to-back, not side by side.
    stack = np.full((40, 120, 120), -1000.0, dtype=np.float32)
    yy2, xx2 = np.mgrid[0:120, 0:120]
    body2 = ((xx2 - 60.0) ** 2 + (yy2 - 60.0) ** 2) <= 50.0 ** 2
    # keep the low-density slabs inside a tissue rim: real lungs are enclosed
    # by the chest wall on every axial slice, so a phantom whose "lungs" reach
    # the body surface is not representative
    inner = ((xx2 - 60.0) ** 2 + (yy2 - 60.0) ** 2) <= 40.0 ** 2
    front = inner & (yy2 < 52)          # anterior slab
    back = inner & (yy2 > 68)           # posterior slab - same side-to-side extent
    for k in range(40):
        stack[k][body2] = 40.0
        if 4 <= k < 36:
            stack[k][front] = -800.0
            stack[k][back] = -800.0
    sres = segment_lungs(stack, [True] * 40, (1.0, 1.0, 2.5),
                          col_cosines=(1.0, 0.0, 0.0), orientation_reliable=True)
    check('stacked (non-lateralised) regions segment successfully', sres.success, sres.warnings)
    check('non-lateralised regions are detected as such',
          sres.stats.get('component_lateral_separation', 1.0) < 0.25,
          sres.stats.get('component_lateral_separation'))
    check('non-lateralised regions are NOT labelled left/right by component',
          sres.stats.get('left_right_method') == 'sagittal_midline_split',
          sres.stats.get('left_right_method'))
    check('the rejection is explained to the reader',
          any('not laterally separated' in w for w in sres.warnings), sres.warnings)
    if sres.left_mask is not None:
        lc = np.argwhere(sres.left_mask.any(axis=(0, 1))).ravel()
        rc = np.argwhere(sres.right_mask.any(axis=(0, 1))).ravel()
        check('after the midline fallback the sides no longer overlap',
              lc.min() > rc.max(), (int(rc.max()), int(lc.min())))

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
