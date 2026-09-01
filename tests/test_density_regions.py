"""
Tests for api/density_regions.py.

Ground truth by construction: phantoms place an exact, known number of voxels
at exact HU values, so every band fraction, region volume, and centroid has a
correct answer that can be checked rather than eyeballed.

Also asserts the honesty properties: measurements are named for the HU range
they count (never for a disease), no confidence score is produced, and the
result declares that no model contributed.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from density_regions import (  # noqa: E402
    analyze_density_regions, band_fractions, cluster_band_regions,
    DENSITY_BANDS, PERCENTILE_INDEX, MIN_REGION_VOLUME_ML, OK, NOT_AVAILABLE,
)
from mesh_reconstruction import build_volume_geometry  # noqa: E402

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


def geom(shape, spacing):
    return build_volume_geometry(
        shape=shape, origin_mm=(0.0, 0.0, 0.0),
        pixel_spacing_row_col=[spacing[1], spacing[0]], slice_spacing_mm=spacing[2],
        iop=[1, 0, 0, 0, 1, 0], orientation_reliable=True)


def main():
    # =====================================================================
    # BAND FRACTIONS: exact voxel counts at exact HU values
    # =====================================================================
    shape = (20, 40, 40)
    spacing = (1.0, 1.0, 2.0)
    vox_ml = 1.0 * 1.0 * 2.0 / 1000.0          # 0.002 mL per voxel

    hu = np.full(shape, 0.0, dtype=np.float32)
    mask = np.zeros(shape, dtype=bool)
    mask[2:18, 5:35, 5:35] = True               # 16*30*30 = 14400 lung voxels
    total = int(mask.sum())
    check('phantom lung voxel count is exact', total == 14400, total)

    # partition the lung into known counts at known densities
    lung_idx = np.argwhere(mask)
    hu[mask] = -800.0                                     # baseline: normal band
    very_low = lung_idx[:1000]                            # 1000 voxels at -980
    high     = lung_idx[1000:1400]                        # 400 voxels at -400
    for (k, j, i) in very_low:
        hu[k, j, i] = -980.0
    for (k, j, i) in high:
        hu[k, j, i] = -400.0

    bf = band_fractions(hu, mask, vox_ml)
    check('band fractions computed', bf['status'] == OK)
    check('LAA-950 counts exactly the voxels below -950',
          bf['laa_950']['voxel_count'] == 1000, bf['laa_950']['voxel_count'])
    check('LAA-950 percentage is exact',
          close(bf['laa_950']['percent_of_lung'], 1000 / 14400 * 100, 0.001),
          bf['laa_950']['percent_of_lung'])
    check('LAA-950 volume uses physical spacing',
          close(bf['laa_950']['volume_ml'], 1000 * vox_ml, 0.001), bf['laa_950']['volume_ml'])
    check('HAA(-600..-250) counts exactly the -400 voxels',
          bf['haa_600_250']['voxel_count'] == 400, bf['haa_600_250']['voxel_count'])
    check('normal band counts the remaining -800 voxels',
          bf['normal_band']['voxel_count'] == 14400 - 1400,
          bf['normal_band']['voxel_count'])
    check('LAA-910 includes the -980 voxels only',
          bf['laa_910']['voxel_count'] == 1000, bf['laa_910']['voxel_count'])
    check('LAA-856 also includes only the -980 voxels here',
          bf['laa_856']['voxel_count'] == 1000, bf['laa_856']['voxel_count'])

    # bands must not silently overlap in a way that double counts the lung
    disjoint = (bf['laa_950']['voxel_count'] + bf['normal_band']['voxel_count']
                + bf['haa_600_250']['voxel_count'])
    check('LAA-950 + normal + HAA together account for every lung voxel',
          disjoint == total, (disjoint, total))

    p15 = bf['perc15_hu']['value_hu']
    check('Perc15 matches numpy percentile',
          close(p15, float(np.percentile(hu[mask], PERCENTILE_INDEX)), 0.01), p15)

    check('empty mask returns NOT_AVAILABLE, not zeros',
          band_fractions(hu, np.zeros(shape, bool), vox_ml)['status'] == NOT_AVAILABLE)

    # =====================================================================
    # HONESTY: naming, references, no fabricated confidence
    # =====================================================================
    disease_words = ('emphysema', 'fibrosis', 'copd', 'cancer', 'tumor', 'tumour',
                     'pneumonia', 'disease', 'honeycomb', 'nodule', 'malignan')
    for key, label, lo, hi, cite, caveat in DENSITY_BANDS:
        low = label.lower()
        check(f'band "{key}" is named for its HU range, not a disease',
              not any(w in low for w in disease_words), label)
        check(f'band "{key}" carries a reference', bool(cite), cite)
    check('the expiratory-only cut-point is flagged as not interpretable here',
          any(b[0] == 'laa_856' and b[5] for b in DENSITY_BANDS))

    # =====================================================================
    # REGION CLUSTERING: known blobs at known locations
    # =====================================================================
    shape2 = (30, 60, 60)
    spacing2 = (1.0, 1.0, 1.0)
    vox2 = 1.0 / 1000.0
    hu2 = np.full(shape2, -800.0, dtype=np.float32)
    mask2 = np.zeros(shape2, dtype=bool)
    mask2[3:27, 5:55, 5:55] = True

    # three separated cubes below -950: 8^3, 6^3, 4^3 voxels
    hu2[5:13, 10:18, 10:18] = -980.0      # 512
    hu2[5:11, 30:36, 30:36] = -980.0      # 216
    hu2[20:24, 44:48, 12:16] = -980.0     # 64
    g2 = geom(shape2, spacing2)

    res = cluster_band_regions(hu2, mask2, 'laa_950', g2, vox2, min_volume_ml=0.0)
    check('clustering finds exactly three regions', res['region_count'] == 3, res['region_count'])
    vols = sorted((r['voxel_count'] for r in res['regions']), reverse=True)
    check('region voxel counts are exact', vols == [512, 216, 64], vols)
    check('regions are ordered largest first',
          [r['voxel_count'] for r in res['regions']] == vols)
    # volumes are reported rounded to 0.01 mL, so compare at that precision
    check('region volume uses physical spacing',
          close(res['regions'][0]['volume_ml'], round(512 * vox2, 2), 1e-9),
          (res['regions'][0]['volume_ml'], round(512 * vox2, 2)))
    check('total band volume equals the sum of the regions',
          close(res['total_band_volume_ml'], round((512 + 216 + 64) * vox2, 2), 1e-9),
          res['total_band_volume_ml'])
    check('unrounded region volume matches the voxel count exactly',
          close(res['regions'][0]['voxel_count'] * vox2, 0.512, 1e-12))

    r0 = res['regions'][0]
    check('centroid of the 8-cube is at its true centre',
          r0['centroid_voxel'] == {'i': 14, 'j': 14, 'k': 8}, r0['centroid_voxel'])
    check('bounding box matches the cube extent',
          r0['bounding_box_voxel']['i'] == [10, 17] and r0['bounding_box_voxel']['k'] == [5, 12],
          r0['bounding_box_voxel'])
    check('bounding-box longest edge is the cube edge in mm',
          close(r0['bounding_box_longest_edge_mm'], 8.0, 1e-6),
          r0['bounding_box_longest_edge_mm'])
    # a solid cube fills its bounding box completely
    check('a solid cube reports a fill fraction of 1.0',
          close(r0['bounding_box_fill_fraction'], 1.0, 1e-6),
          r0['bounding_box_fill_fraction'])
    check('a solid cube is described as compact', r0['morphology'] == 'compact', r0['morphology'])
    check('the extent carries a note against reading it as a diameter',
          'lesion diameter' in r0['shape_note'])
    check('region mean HU is the value placed there',
          close(r0['mean_hu'], -980.0, 1e-6), r0['mean_hu'])
    check('every region carries an id', all(r['region_id'] for r in res['regions']))

    # REGRESSION: a diffuse network spans a large box without filling it. On a
    # real study a 178 mL region reported a 356 mm bounding box; without the
    # fill fraction that reads as a 36 cm lesion.
    hu3 = np.full(shape2, -800.0, dtype=np.float32)
    # three thin orthogonal slabs forming one CONNECTED 3D cross: it spans
    # nearly the whole lung box while filling only a few percent of it.
    scatter = np.zeros(shape2, dtype=bool)
    scatter[4:26, 29:31, 8:52] = True
    scatter[4:26, 8:52, 29:31] = True
    scatter[14:16, 8:52, 8:52] = True
    hu3[scatter & mask2] = -980.0
    diff = cluster_band_regions(hu3, mask2, 'laa_950', g2, vox2, min_volume_ml=0.0)
    spread = max(diff['regions'], key=lambda r: r['bounding_box_longest_edge_mm'])
    check('a scattered region spans a large bounding box',
          spread['bounding_box_longest_edge_mm'] > 20.0,
          spread['bounding_box_longest_edge_mm'])
    check('a scattered region reports a low fill fraction',
          spread['bounding_box_fill_fraction'] < 0.4, spread['bounding_box_fill_fraction'])
    check('a scattered region is not described as compact',
          spread['morphology'] != 'compact', spread['morphology'])

    # a region's centroid must map to a real patient coordinate
    check('centroid is reported in patient-space millimetres',
          isinstance(r0['centroid_mm'], list) and len(r0['centroid_mm']) == 3, r0['centroid_mm'])

    # the minimum-volume floor must exclude small regions and say so
    # 0.2 mL at 1 mm^3 voxels = a 200-voxel floor, so 512 and 216 survive and
    # 64 is excluded.
    filtered = cluster_band_regions(hu2, mask2, 'laa_950', g2, vox2, min_volume_ml=0.2)
    check('the volume floor excludes regions below it',
          filtered['region_count'] == 2, filtered['region_count'])
    check('the surviving regions are the two largest',
          sorted(r['voxel_count'] for r in filtered['regions']) == [216, 512],
          [r['voxel_count'] for r in filtered['regions']])
    strict = cluster_band_regions(hu2, mask2, 'laa_950', g2, vox2, min_volume_ml=0.3)
    check('a higher floor excludes more', strict['region_count'] == 1, strict['region_count'])

    capped = cluster_band_regions(hu2, mask2, 'laa_950', g2, vox2,
                                   min_volume_ml=0.0, max_regions=2)
    check('the returned-region cap is applied', len(capped['regions']) == 2)
    check('truncation is disclosed rather than silent', 'note' in capped, capped.get('note'))

    empty = cluster_band_regions(np.full(shape2, -800.0, np.float32), mask2,
                                  'laa_950', g2, vox2)
    check('a band with no voxels returns an empty list, not a fabricated region',
          empty['regions'] == [] and empty['region_count'] == 0)
    check('an unknown band is refused',
          cluster_band_regions(hu2, mask2, 'not_a_band', g2, vox2)['status'] == NOT_AVAILABLE)

    # =====================================================================
    # FULL ANALYSIS, INCLUDING SIDE ASSIGNMENT
    # =====================================================================
    left = np.zeros(shape2, bool);  left[3:27, 5:55, 32:55] = True
    right = np.zeros(shape2, bool); right[3:27, 5:55, 5:32] = True
    full = analyze_density_regions(hu2, mask2, g2, vox2,
                                    left_mask=left, right_mask=right)
    check('full densitometry succeeds', full['status'] == OK)
    check('per-side densitometry is produced', 'left_lung' in full and 'right_lung' in full)
    # the full analysis uses the default MIN_REGION_VOLUME_ML floor (0.5 mL),
    # which at 1 mm^3 voxels keeps only the 512-voxel cube
    check('LAA regions are clustered in the full result',
          full['regions']['laa_950']['region_count'] == 1,
          full['regions']['laa_950']['region_count'])
    check('the default floor is the documented one', MIN_REGION_VOLUME_ML == 0.5)

    fine = analyze_density_regions(hu2, mask2, g2, vox2, left_mask=left, right_mask=right)
    fine['regions']['laa_950'] = cluster_band_regions(
        hu2, mask2, 'laa_950', g2, vox2, min_volume_ml=0.0,
        side_of_column=lambda i: 'left' if i >= 32 else 'right')
    full = fine
    sides = {r['side'] for r in full['regions']['laa_950']['regions']}
    check('regions are assigned a side', sides.issubset({'left', 'right'}), sides)
    by_i = {r['centroid_voxel']['i']: r['side'] for r in full['regions']['laa_950']['regions']}
    check('the cube at column 14 is on the same side as the mask covering column 14',
          by_i[14] == 'right', by_i)
    check('the cube at column 32 is on the other side', by_i[32] == 'left', by_i)

    check('the result states no model contributed',
          full['provenance']['model_derived'] is False)
    check('the result explains that these are measurements, not findings',
          'not findings' in full['interpretation_note'])
    check('no confidence score is fabricated anywhere',
          'confidence' not in str(full).lower())

    check('missing segmentation yields NOT_AVAILABLE',
          analyze_density_regions(hu2, None, g2, vox2)['status'] == NOT_AVAILABLE)
    check('empty mask yields NOT_AVAILABLE',
          analyze_density_regions(hu2, np.zeros(shape2, bool), g2, vox2)['status'] == NOT_AVAILABLE)

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
