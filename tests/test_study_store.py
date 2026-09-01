"""
Tests for the durable, memory-bounded study store.

The two properties that motivated it:
  1. A study survives losing the in-memory state (process restart).
  2. Memory stays bounded regardless of how many studies are stored.

Also verifies that reconstructed volumes, segmentation masks, geometry, and
ownership all round-trip losslessly, and that deletion actually removes the
pixel data from disk.

Synthetic arrays only - no DICOM I/O, no patient data.
"""
import os
import shutil
import sys

import numpy as np

STORE = '/tmp/vitalitysync_test_store'
os.environ['STUDY_STORE_PATH'] = STORE

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from study_store import StudyStore  # noqa: E402
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


def build_phantom(n_slices=30, rows=100, cols=100, spacing=(1.4, 1.4, 4.0),
                   body_r=55.0, lung_r=20.0, lung_off=28.0):
    """Same geometry as the phantom used elsewhere in the suite, which yields
    ~241 mL of 'lung' - comfortably above the segmentation plausibility floor.
    Smaller variants are used only where segmentation is not exercised."""
    sx, sy, _ = spacing
    cx, cy = cols / 2.0, rows / 2.0
    yy, xx = np.mgrid[0:rows, 0:cols]
    dx, dy = (xx - cx) * sx, (yy - cy) * sy
    body = (dx ** 2 + dy ** 2) <= body_r ** 2
    lungL = ((dx - lung_off) ** 2 + dy ** 2) <= lung_r ** 2
    lungR = ((dx + lung_off) ** 2 + dy ** 2) <= lung_r ** 2
    vol = np.full((n_slices, rows, cols), -1000.0, dtype=np.float32)
    for k in range(n_slices):
        vol[k][body] = 40.0
        if 3 <= k < n_slices - 3:
            vol[k][lungL] = -800.0
            vol[k][lungR] = -800.0
    return vol


def make_study(volume, spacing=(1.4, 1.4, 4.0), owner=7):
    geom = build_volume_geometry(
        shape=volume.shape, origin_mm=(10.0, -5.0, 2.0),
        pixel_spacing_row_col=[spacing[1], spacing[0]], slice_spacing_mm=spacing[2],
        iop=[1, 0, 0, 0, 1, 0], orientation_reliable=True,
    )
    return {
        'hu_volume': volume,
        'hu_available_per_slice': [True] * volume.shape[0],
        'pixel_spacing': [spacing[1], spacing[0]],
        'slice_spacing': spacing[2],
        'summary': {'slice_count': int(volume.shape[0]), 'hu_conversion_status': 'AVAILABLE'},
        'geometry': geom,
        'segmentation': None,
        'owner_doctor_id': owner,
        'created_at': '2026-01-01T00:00:00+00:00',
    }


def main():
    if os.path.isdir(STORE):
        shutil.rmtree(STORE)

    store = StudyStore(path=STORE, cache_size=2)
    volume = build_phantom()
    study = make_study(volume)

    store.put('study-a', study)
    check('study is written to disk', os.path.isdir(os.path.join(STORE, 'study-a')))
    check('volume file exists', os.path.exists(os.path.join(STORE, 'study-a', 'volume.npy')))
    check('study reports as present', 'study-a' in store)

    # ---------- survives losing all in-memory state ----------
    fresh = StudyStore(path=STORE, cache_size=2)   # simulates a process restart
    loaded = fresh.get('study-a')
    check('study survives a simulated restart', loaded is not None)
    check('volume round-trips exactly',
          np.array_equal(np.asarray(loaded['hu_volume']), volume))
    check('ownership round-trips', loaded['owner_doctor_id'] == 7, loaded['owner_doctor_id'])
    check('summary round-trips', loaded['summary']['slice_count'] == volume.shape[0])
    check('hu_available_per_slice round-trips',
          loaded['hu_available_per_slice'] == [True] * volume.shape[0])

    g = loaded['geometry']
    check('geometry object is rebuilt', g is not None)
    check('geometry spacing round-trips', tuple(g.spacing_mm) == (1.4, 1.4, 4.0), g.spacing_mm)
    check('geometry origin round-trips', tuple(g.origin_mm) == (10.0, -5.0, 2.0), g.origin_mm)
    check('geometry orientation flag round-trips', g.orientation_reliable is True)
    check('geometry transform still works after reload',
          [round(v, 6) for v in g.to_world(0, 0, 0)] == [10.0, -5.0, 2.0], g.to_world(0, 0, 0))

    # ---------- volume is memory-mapped, not fully resident ----------
    check('reloaded volume is memory-mapped', isinstance(loaded['hu_volume'], np.memmap),
          type(loaded['hu_volume']).__name__)
    # a slice read through the mmap must still produce correct values
    check('slice read through mmap matches source',
          np.array_equal(np.asarray(loaded['hu_volume'][3]), volume[3]))

    # ---------- segmentation persists ----------
    seg = segment_lungs(volume, [True] * volume.shape[0], (1.4, 1.4, 4.0),
                         col_cosines=(1.0, 0.0, 0.0), orientation_reliable=True)
    check('phantom segmentation succeeds (precondition)', seg.success, seg.warnings)

    store.save_segmentation('study-a', seg)
    fresh2 = StudyStore(path=STORE, cache_size=2)
    reloaded = fresh2.get('study-a')
    rseg = reloaded['segmentation']
    check('segmentation survives a restart', rseg is not None)
    check('segmentation success flag round-trips', rseg.success is True)
    check('segmentation method label round-trips', rseg.method == seg.method)
    check('segmentation stats round-trip',
          abs(rseg.stats['lung_volume_ml'] - seg.stats['lung_volume_ml']) < 1e-6)
    check('combined mask round-trips exactly (bit-packed)',
          np.array_equal(rseg.mask, seg.mask))
    check('left mask round-trips exactly', np.array_equal(rseg.left_mask, seg.left_mask))
    check('right mask round-trips exactly', np.array_equal(rseg.right_mask, seg.right_mask))
    check('rehydrated segmentation exposes to_public_dict',
          rseg.to_public_dict()['left_right_available'] is True)

    # bit-packing really is smaller than a raw bool array
    seg_bytes = os.path.getsize(os.path.join(STORE, 'study-a', 'segmentation.npz'))
    raw_bytes = seg.mask.size * 3  # three bool masks, 1 byte per element
    check('segmentation on disk is smaller than raw bool masks',
          seg_bytes < raw_bytes, (seg_bytes, raw_bytes))

    # ---------- memory stays bounded ----------
    bounded = StudyStore(path=STORE, cache_size=2)
    for i in range(6):
        bounded.put(f'bulk-{i}', make_study(build_phantom(n_slices=8, rows=24, cols=24)))
    check('cache never exceeds its size limit', bounded.resident_count <= 2, bounded.resident_count)
    check('all studies remain retrievable despite eviction',
          all(bounded.get(f'bulk-{i}') is not None for i in range(6)))
    check('store reports every stored study', len(bounded) >= 7, len(bounded))

    # evicted-then-reloaded study is still correct
    revived = bounded.get('bulk-0')
    check('evicted study reloads with correct shape',
          tuple(np.asarray(revived['hu_volume']).shape) == (8, 24, 24),
          np.asarray(revived['hu_volume']).shape)

    # ---------- deletion removes pixel data from disk ----------
    bounded.pop('bulk-0')
    check('deleted study is gone from the store', 'bulk-0' not in bounded)
    check('deleted study directory is removed from disk',
          not os.path.isdir(os.path.join(STORE, 'bulk-0')))
    check('deleting a missing study returns the default',
          bounded.pop('never-existed', 'sentinel') == 'sentinel')

    # ---------- unsafe ids are rejected ----------
    for bad in ('../escape', 'a/b', 'a\\b'):
        check(f'unsafe study id rejected: {bad!r}', bad not in bounded)
        check(f'unsafe study id get returns default: {bad!r}', bounded.get(bad) is None)

    # ---------- a study with no volume is handled ----------
    bounded.put('meta-only', {
        'hu_volume': None, 'hu_available_per_slice': [True], 'pixel_spacing': [1, 1],
        'slice_spacing': 1, 'summary': {}, 'geometry': None, 'segmentation': None,
        'owner_doctor_id': 3, 'created_at': 'now',
    })
    mo = StudyStore(path=STORE, cache_size=2).get('meta-only')
    check('volume-less study round-trips', mo is not None and mo['hu_volume'] is None)
    check('volume-less study keeps ownership', mo['owner_doctor_id'] == 3)

    shutil.rmtree(STORE, ignore_errors=True)

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
