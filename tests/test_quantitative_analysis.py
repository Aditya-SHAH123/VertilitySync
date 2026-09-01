"""
Tests for api/quantitative_analysis.py.

The approach throughout is ground truth by construction: phantoms are built so
the correct answer is known analytically (an exact voxel count, an exact HU
value, an exact percentile), and the computed value is compared against it.
A metric that only "looks plausible" is not considered verified.

Synthetic data only. No DICOM I/O, no patient data.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from quantitative_analysis import (  # noqa: E402
    analyze_study, hu_statistics, hu_histogram, superior_inferior_zones,
    pleural_distance_regions, assess_scan_quality,
    OK, WARNING, FAILED, NOT_AVAILABLE, NOT_EVALUATED,
    HIST_BIN_WIDTH_HU, HIST_MIN_HU, HIST_MAX_HU, PERIPHERAL_BAND_MM,
)
from mesh_reconstruction import build_volume_geometry  # noqa: E402
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


def close(a, b, tol=1e-6):
    return a is not None and abs(float(a) - float(b)) <= tol


class FakeSeg:
    """Minimal stand-in matching the attributes analyze_study reads."""
    def __init__(self, mask, left=None, right=None, success=True, stats=None, warnings=None):
        self.mask, self.left_mask, self.right_mask = mask, left, right
        self.success = success
        self.status = OK
        self.method = 'test-rule-based'
        self.method_version = '0.0.0-test'
        self.stats = stats or {}
        self.warnings = warnings or []


def make_geometry(shape, spacing):
    return build_volume_geometry(
        shape=shape, origin_mm=(0.0, 0.0, 0.0),
        pixel_spacing_row_col=[spacing[1], spacing[0]], slice_spacing_mm=spacing[2],
        iop=[1, 0, 0, 0, 1, 0], orientation_reliable=True)


def base_summary(shape, spacing):
    return {
        'slice_count': shape[0], 'rows': shape[1], 'columns': shape[2],
        'pixel_spacing_mm': [spacing[1], spacing[0]], 'slice_spacing_mm': spacing[2],
        'slice_thickness_mm': spacing[2], 'hu_conversion_status': 'AVAILABLE',
        'orientation_status': 'AVAILABLE', 'series_status': 'PASS',
        'validation_warnings': [], 'scanner_metadata': {'Manufacturer': 'SyntheticGen'},
    }


def main():
    # =====================================================================
    # VOLUME: exact voxel count x exact voxel volume
    # =====================================================================
    spacing = (0.5, 2.0, 4.0)                    # deliberately anisotropic
    voxel_mm3 = 0.5 * 2.0 * 4.0                  # = 4.0 mm^3
    shape = (10, 20, 20)
    mask = np.zeros(shape, dtype=bool)
    mask[2:8, 5:15, 5:15] = True                 # exactly 6*10*10 = 600 voxels
    expected_voxels = 600
    expected_ml = expected_voxels * voxel_mm3 / 1000.0    # = 2.4 mL

    check('phantom voxel count is exact', int(mask.sum()) == expected_voxels, int(mask.sum()))

    hu = np.full(shape, -900.0, dtype=np.float32)
    hu[mask] = -800.0
    stats = hu_statistics(hu, mask, voxel_mm3 / 1000.0)
    check('volume uses physical spacing, not voxel count',
          close(stats['volume_ml'], expected_ml, 1e-3), (stats['volume_ml'], expected_ml))
    check('voxel count is reported exactly', stats['voxel_count'] == expected_voxels)

    # anisotropy must matter: same count, different spacing -> different volume
    s2 = hu_statistics(hu, mask, (1.0 * 1.0 * 1.0) / 1000.0)
    check('different spacing yields a different volume for the same mask',
          not close(s2['volume_ml'], stats['volume_ml'], 1e-6), (s2['volume_ml'], stats['volume_ml']))

    # =====================================================================
    # HU STATISTICS: analytically known distribution
    # =====================================================================
    vals = np.arange(-1000, -900, dtype=np.float32)      # 100 values, -1000..-901
    hu2 = np.zeros((1, 10, 10), dtype=np.float32)
    hu2[0] = vals.reshape(10, 10)
    m2 = np.ones((1, 10, 10), dtype=bool)
    st = hu_statistics(hu2, m2, 1.0)
    check('mean matches the analytic mean', close(st['mean_hu'], vals.mean(), 0.01), st['mean_hu'])
    check('median matches numpy', close(st['median_hu'], float(np.median(vals)), 0.01), st['median_hu'])
    check('std is population std (ddof=0)', close(st['std_hu'], float(vals.std(ddof=0)), 0.01), st['std_hu'])
    check('min matches', close(st['min_hu'], -1000, 1e-6), st['min_hu'])
    check('max matches', close(st['max_hu'], -901, 1e-6), st['max_hu'])
    for p in (5, 25, 50, 75, 95):
        check(f'p{p} matches numpy percentile',
              close(st['percentiles_hu'][f'p{p}'], float(np.percentile(vals, p)), 0.01),
              st['percentiles_hu'][f'p{p}'])
    check('uniform ramp has near-zero skewness', abs(st['skewness']) < 0.01, st['skewness'])
    check('excess kurtosis of a uniform ramp is about -1.2',
          abs(st['kurtosis_excess'] + 1.2) < 0.05, st['kurtosis_excess'])

    # constant region: zero spread, defined moments
    const = np.full((1, 5, 5), -700.0, dtype=np.float32)
    cs = hu_statistics(const, np.ones((1, 5, 5), bool), 1.0)
    check('constant region has zero std', close(cs['std_hu'], 0.0, 1e-9), cs['std_hu'])
    check('constant region skewness is defined (0)', cs['skewness'] == 0.0)

    check('empty mask returns NOT_AVAILABLE, not a number',
          hu_statistics(hu, np.zeros(shape, bool), 1.0)['status'] == NOT_AVAILABLE)

    # =====================================================================
    # HISTOGRAM: counts must be exact and conserve voxels
    # =====================================================================
    h = hu_histogram(hu2, m2)
    n_bins = int(round((HIST_MAX_HU - HIST_MIN_HU) / HIST_BIN_WIDTH_HU))
    check('histogram bin count matches the fixed grid', len(h['counts']) == n_bins, len(h['counts']))
    check('histogram edges are one longer than counts', len(h['bin_edges_hu']) == n_bins + 1)
    check('every voxel is accounted for (in bins + under + over)',
          sum(h['counts']) + h['underflow_count'] + h['overflow_count'] == h['total_voxels'],
          (sum(h['counts']), h['underflow_count'], h['overflow_count'], h['total_voxels']))
    check('histogram total equals the region voxel count', h['total_voxels'] == 100)

    # out-of-range values are counted, not silently dropped
    wide = np.array([[[-3000.0, 900.0, -500.0]]], dtype=np.float32)
    hw = hu_histogram(wide, np.ones((1, 1, 3), bool))
    check('underflow is counted', hw['underflow_count'] == 1, hw['underflow_count'])
    check('overflow is counted', hw['overflow_count'] == 1, hw['overflow_count'])
    check('in-range value is binned', sum(hw['counts']) == 1, sum(hw['counts']))

    # =====================================================================
    # ZONES: split along the true superior-inferior axis
    # =====================================================================
    zshape = (30, 10, 10)
    zmask = np.zeros(zshape, dtype=bool)
    zmask[:, 2:8, 2:8] = True                     # uniform column through all slices
    zgeom = make_geometry(zshape, (1.0, 1.0, 1.0))
    zones, zmeta = superior_inferior_zones(zmask, zgeom)
    check('zones are produced', zones is not None and zmeta['status'] == OK)
    counts = {k: int(v.sum()) for k, v in zones.items()}
    check('zones partition the mask exactly once',
          sum(counts.values()) == int(zmask.sum()), (counts, int(zmask.sum())))
    check('zones do not overlap',
          not (zones['upper'] & zones['middle']).any() and not (zones['middle'] & zones['lower']).any())
    check('a uniform column splits into roughly equal thirds',
          max(counts.values()) - min(counts.values()) <= int(zmask.sum()) * 0.12, counts)

    # upper must be at higher world Z than lower
    up_k = np.argwhere(zones['upper'].any(axis=(1, 2))).ravel()
    lo_k = np.argwhere(zones['lower'].any(axis=(1, 2))).ravel()
    check('upper zone is superior to lower zone (world Z, not slice index)',
          zgeom.to_world(int(up_k.mean()), 0, 0)[2] > zgeom.to_world(int(lo_k.mean()), 0, 0)[2])

    # reversed slice direction must give the same anatomical assignment
    rev_geom = build_volume_geometry(
        shape=zshape, origin_mm=(0.0, 0.0, 29.0),
        pixel_spacing_row_col=[1.0, 1.0], slice_spacing_mm=1.0,
        iop=[1, 0, 0, 0, 1, 0], orientation_reliable=True)
    rev_geom.slice_cosines = (0.0, 0.0, -1.0)     # slices march inferiorly
    rzones, _ = superior_inferior_zones(zmask, rev_geom)
    r_up = np.argwhere(rzones['upper'].any(axis=(1, 2))).ravel()
    check('reversed slice direction still puts "upper" at superior Z',
          rev_geom.to_world(int(r_up.mean()), 0, 0)[2] > rev_geom.to_world(int(np.argwhere(
              rzones['lower'].any(axis=(1, 2))).ravel().mean()), 0, 0)[2])

    check('empty mask yields NOT_AVAILABLE zones',
          superior_inferior_zones(np.zeros(zshape, bool), zgeom)[1]['status'] == NOT_AVAILABLE)

    # =====================================================================
    # PLEURAL BANDS: distance transform in millimetres
    # =====================================================================
    pshape = (1, 101, 101)
    pmask = np.zeros(pshape, dtype=bool)
    yy, xx = np.mgrid[0:101, 0:101]
    pmask[0] = ((yy - 50) ** 2 + (xx - 50) ** 2) <= 45 ** 2     # radius 45 voxels
    pspacing = (1.0, 1.0, 1.0)                                   # 1 mm isotropic
    bands, bmeta = pleural_distance_regions(pmask, pspacing)
    check('pleural bands are produced', bands is not None and bmeta['status'] == OK)
    check('bands partition the lung exactly',
          int(bands['peripheral'].sum()) + int(bands['central'].sum()) == int(pmask.sum()),
          (int(bands['peripheral'].sum()), int(bands['central'].sum()), int(pmask.sum())))
    check('subpleural is a subset of peripheral',
          bool((bands['subpleural'] & ~bands['peripheral']).sum() == 0))
    # central core of a radius-45 disc with a 20 mm band = radius-25 disc
    expected_central = int((((yy - 50) ** 2 + (xx - 50) ** 2) <= (45 - PERIPHERAL_BAND_MM) ** 2).sum())
    got_central = int(bands['central'].sum())
    check('central region matches the analytic inner disc within 3%',
          abs(got_central - expected_central) <= 0.03 * expected_central,
          (got_central, expected_central))
    check('max depth is about the disc radius',
          abs(bmeta['max_depth_mm'] - 45.0) < 2.0, bmeta['max_depth_mm'])

    # anisotropic spacing must change the physical distance
    bands2, bmeta2 = pleural_distance_regions(pmask, (2.0, 2.0, 1.0))
    check('distance transform honours anisotropic spacing',
          bmeta2['max_depth_mm'] > bmeta['max_depth_mm'] * 1.5, (bmeta2['max_depth_mm'], bmeta['max_depth_mm']))

    # REGRESSION: internal vessel holes must not act as false pleural surfaces.
    # A real lung mask keeps vessel/airway holes (they reach the hilum and so
    # survive 3D hole filling). Measuring distance to those holes rather than
    # to the pleura capped the depth at 21.8 mm on a real study and called
    # 99.98% of the lung peripheral.
    holed = pmask.copy()
    for (cy_, cx_) in ((35, 35), (60, 40), (45, 62), (55, 58), (38, 55)):
        holed[0][((yy - cy_) ** 2 + (xx - cx_) ** 2) <= 3 ** 2] = False
    check('the holed phantom really does contain interior holes',
          int(pmask.sum()) - int(holed.sum()) > 100, int(pmask.sum()) - int(holed.sum()))
    hbands, hmeta = pleural_distance_regions(holed, pspacing)
    check('interior holes do not shrink the measured depth to the pleura',
          hmeta['max_depth_mm'] > 0.85 * bmeta['max_depth_mm'],
          (hmeta['max_depth_mm'], bmeta['max_depth_mm']))
    check('interior holes do not collapse the central region',
          int(hbands['central'].sum()) > 0.85 * int(bands['central'].sum()),
          (int(hbands['central'].sum()), int(bands['central'].sum())))
    check('the envelope correction is reported',
          hmeta['envelope_voxel_excess_percent'] > 0, hmeta['envelope_voxel_excess_percent'])
    check('statistics are still reported over the original mask, not the envelope',
          int(hbands['peripheral'].sum()) + int(hbands['central'].sum()) == int(holed.sum()),
          (int(hbands['peripheral'].sum()) + int(hbands['central'].sum()), int(holed.sum())))

    # =====================================================================
    # FULL ANALYSIS on a phantom with known left/right volumes
    # =====================================================================
    fshape = (24, 60, 60)
    fspacing = (1.0, 1.0, 2.0)
    fvox_ml = 1.0 * 1.0 * 2.0 / 1000.0
    fhu = np.full(fshape, -950.0, dtype=np.float32)
    lmask = np.zeros(fshape, dtype=bool)
    rmask = np.zeros(fshape, dtype=bool)
    lmask[4:20, 20:40, 34:50] = True     # 16*20*16 = 5120 voxels
    rmask[4:20, 20:40, 10:26] = True     # 5120 voxels
    full = lmask | rmask
    fhu[lmask] = -800.0
    fhu[rmask] = -700.0
    fgeom = make_geometry(fshape, fspacing)
    seg = FakeSeg(full, lmask, rmask, stats={'left_right_method': 'component_labelling'})
    res = analyze_study(fhu, [True] * fshape[0], fgeom, seg, base_summary(fshape, fspacing))

    lm = res['lung_metrics']
    check('total lung volume is exact',
          close(lm['total_lung_volume_ml'], 10240 * fvox_ml, 0.05), lm['total_lung_volume_ml'])
    check('left lung volume is exact',
          close(lm['left_lung_volume_ml'], 5120 * fvox_ml, 0.05), lm['left_lung_volume_ml'])
    check('right lung volume is exact',
          close(lm['right_lung_volume_ml'], 5120 * fvox_ml, 0.05), lm['right_lung_volume_ml'])
    # Each volume is independently rounded to 0.1 mL, so the per-side values
    # can differ from the total by up to half a step each - 0.15 mL across the
    # three figures. Anything larger would mean the masks disagree.
    check('left + right equals total, within rounding',
          close(lm['left_lung_volume_ml'] + lm['right_lung_volume_ml'], lm['total_lung_volume_ml'], 0.16),
          (lm['left_lung_volume_ml'], lm['right_lung_volume_ml'], lm['total_lung_volume_ml']))
    check('left + right equals total exactly before rounding',
          int(seg.left_mask.sum()) + int(seg.right_mask.sum()) == int(seg.mask.sum()))
    check('left/right ratio is 1.0 for a symmetric phantom', close(lm['left_right_ratio'], 1.0, 1e-3),
          lm['left_right_ratio'])

    dm = res['density_metrics']
    check('left lung mean HU is exactly the value placed there',
          close(dm['left_lung']['mean_hu'], -800.0, 1e-6), dm['left_lung']['mean_hu'])
    check('right lung mean HU is exactly the value placed there',
          close(dm['right_lung']['mean_hu'], -700.0, 1e-6), dm['right_lung']['mean_hu'])
    check('whole-lung mean is the volume-weighted mean of both sides',
          close(dm['whole_lungs']['mean_hu'], -750.0, 1e-6), dm['whole_lungs']['mean_hu'])

    asym = res['asymmetry']
    check('volume asymmetry index is 0 for a symmetric phantom',
          close(asym['volume_asymmetry_index'], 0.0, 1e-6), asym['volume_asymmetry_index'])
    check('mean HU difference is left minus right',
          close(asym['mean_hu_difference'], -100.0, 1e-6), asym['mean_hu_difference'])

    # asymmetric phantom: index must have the documented sign and bound
    big_left = lmask.copy(); big_left[4:20, 20:40, 30:50] = True   # widen the left lung
    seg2 = FakeSeg(big_left | rmask, big_left, rmask)
    res2 = analyze_study(fhu, [True] * fshape[0], fgeom, seg2, base_summary(fshape, fspacing))
    idx = res2['asymmetry']['volume_asymmetry_index']
    check('a larger left lung gives a positive asymmetry index', idx > 0, idx)
    check('asymmetry index stays within [-1, 1]', -1.0 <= idx <= 1.0, idx)

    # zone percentages must total 100
    zr = res['regional_metrics']['zones']['regions']
    total_pct = sum(z['percent_of_lung'] for z in zr.values())
    check('zone percentages sum to 100', abs(total_pct - 100.0) < 0.5, total_pct)
    zvol = sum(z['volume_ml'] for z in zr.values())
    check('zone volumes sum to the total lung volume',
          abs(zvol - lm['total_lung_volume_ml']) < 0.1, (zvol, lm['total_lung_volume_ml']))

    pb = res['regional_metrics']['pleural_bands']['regions']
    check('peripheral + central sum to the total lung volume',
          abs(pb['peripheral']['volume_ml'] + pb['central']['volume_ml'] - lm['total_lung_volume_ml']) < 0.1)

    # =====================================================================
    # HONEST UNAVAILABILITY - no fabricated sections
    # =====================================================================
    for key in ('lobe_metrics', 'airways', 'vasculature', 'longitudinal'):
        check(f'{key} reports NOT_AVAILABLE', res[key]['status'] == NOT_AVAILABLE, res[key])
        check(f'{key} explains why', len(res[key].get('reason', '')) > 30)
        check(f'{key} contains no numeric measurement',
              not any(isinstance(v, (int, float)) for v in res[key].values()), res[key])

    f = res['findings']
    check('findings report NOT_AVAILABLE', f['status'] == NOT_AVAILABLE)
    check('findings list is empty, not populated with examples', f['regions'] == [] and f['burden'] == {})
    check('finding types are declared for the future contract', len(f['supported_finding_types']) >= 10)

    tr = res['texture_radiomics']
    check('first-order radiomics are marked available', tr['first_order'] == OK)
    for fam in ('glcm', 'glrlm', 'glszm', 'ngtdm', 'gldm', 'shape_features'):
        check(f'{fam} is NOT_AVAILABLE', tr[fam] == NOT_AVAILABLE)

    check('provenance states no model contributed',
          res['provenance_summary']['model_derived_values'] == 'none')
    check('lung metrics carry segmentation provenance',
          lm['provenance']['source'] == 'segmentation')
    check('density metrics carry voxel-computation provenance',
          dm['provenance']['source'] == 'voxel_computation')
    check('density provenance records the percentile method',
          'percentile_method' in dm['provenance'])

    # =====================================================================
    # MISSING / UNRELIABLE SEGMENTATION
    # =====================================================================
    none_res = analyze_study(fhu, [True] * fshape[0], fgeom, None, base_summary(fshape, fspacing))
    check('no segmentation still yields scan quality', none_res['scan_quality']['status'] in (OK, WARNING))
    for key in ('lung_metrics', 'density_metrics', 'regional_metrics', 'asymmetry'):
        check(f'{key} is NOT_AVAILABLE without segmentation',
              none_res[key]['status'] == NOT_AVAILABLE, none_res[key])
        check(f'{key} carries no number without segmentation',
              not any(isinstance(v, (int, float)) for v in none_res[key].values()))

    failed_seg = FakeSeg(full, lmask, rmask, success=False)
    fres = analyze_study(fhu, [True] * fshape[0], fgeom, failed_seg, base_summary(fshape, fspacing))
    check('failed segmentation yields no lung metrics',
          fres['lung_metrics']['status'] == NOT_AVAILABLE)
    check('failed segmentation explains the reason',
          'plausibility' in fres['lung_metrics']['reason'])

    # =====================================================================
    # SCAN QUALITY
    # =====================================================================
    sq = res['scan_quality']
    check('scan quality has an overall status', sq['status'] in (OK, WARNING, FAILED))
    check('scan quality lists individual checks', len(sq['checks']) >= 10, len(sq['checks']))
    names = {c['name'] for c in sq['checks']}
    for expected in ('Slice count', 'Pixel spacing', 'Slice thickness', 'Hounsfield calibration',
                      'Image orientation', 'Reconstruction kernel', 'Contrast status',
                      'Motion / artifact'):
        check(f'quality check present: {expected}', expected in names, names)
    kernel = next(c for c in sq['checks'] if c['name'] == 'Reconstruction kernel')
    check('reconstruction kernel is NOT_AVAILABLE rather than guessed',
          kernel['status'] == NOT_AVAILABLE and kernel['value'] is None)
    motion = next(c for c in sq['checks'] if c['name'] == 'Motion / artifact')
    check('motion is NOT_EVALUATED rather than assumed absent', motion['status'] == NOT_EVALUATED)

    # missing HU calibration must fail the quality gate
    bad = base_summary(fshape, fspacing)
    bad['hu_conversion_status'] = 'NOT AVAILABLE'
    sq_bad = assess_scan_quality(bad, {}, fgeom, 'PASS')
    check('absent HU calibration makes the scan unsuitable',
          sq_bad['suitable_for_quantitative_analysis'] is False, sq_bad['status'])

    trunc = assess_scan_quality(base_summary(fshape, fspacing),
                                 {'touches_scan_range_start_or_end': True}, fgeom, 'PASS')
    check('truncated lungs raise a containment warning',
          any('may not be fully covered' in w for w in trunc['warnings']), trunc['warnings'])

    # =====================================================================
    # END-TO-END with the real segmentation module
    # =====================================================================
    from test_lung_segmentation import build_phantom_volume
    rs = (1.4, 1.4, 4.0)
    rvol = build_phantom_volume(spacing=rs)
    rseg = segment_lungs(rvol, [True] * rvol.shape[0], rs,
                          col_cosines=(1.0, 0.0, 0.0), orientation_reliable=True)
    rgeom = make_geometry(rvol.shape, rs)
    rres = analyze_study(rvol, [True] * rvol.shape[0], rgeom, rseg, base_summary(rvol.shape, rs))
    check('end-to-end analysis succeeds on the segmentation phantom',
          rres['lung_metrics']['status'] == OK, rres['lung_metrics'])
    check('end-to-end volume agrees with the segmentation stat',
          abs(rres['lung_metrics']['total_lung_volume_ml'] - rseg.stats['lung_volume_ml']) < 1.0,
          (rres['lung_metrics']['total_lung_volume_ml'], rseg.stats['lung_volume_ml']))
    check('end-to-end mean HU is the phantom lung value',
          close(rres['density_metrics']['whole_lungs']['mean_hu'], -800.0, 1.0),
          rres['density_metrics']['whole_lungs']['mean_hu'])

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
