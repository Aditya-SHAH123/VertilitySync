"""
Unit tests for api/mesh_reconstruction.py: coordinate transforms and
marching-cubes mesh generation, using synthetic in-memory masks only.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from mesh_reconstruction import (  # noqa: E402
    build_volume_geometry, build_lung_mesh, world_to_voxel_clamped,
    MeshReconstructionError, QUALITY_INTERACTIVE, QUALITY_HIGH_FIDELITY,
)

PASS = []
FAIL = []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'PASS: {name}')
    else:
        FAIL.append(name)
        print(f'FAIL: {name} {extra}')


def sphere_mask(shape=(40, 40, 40), radius=12):
    zz, yy, xx = np.mgrid[0:shape[0], 0:shape[1], 0:shape[2]]
    cz, cy, cx = shape[0] / 2, shape[1] / 2, shape[2] / 2
    return ((zz - cz) ** 2 + (yy - cy) ** 2 + (xx - cx) ** 2) <= radius ** 2


def main():
    shape = (40, 60, 80)

    # --- geometry: reliable orientation, axis-aligned ---
    geom = build_volume_geometry(
        shape=shape, origin_mm=(10.0, -20.0, 5.0),
        pixel_spacing_row_col=[1.5, 2.0],  # [row_spacing, col_spacing]
        slice_spacing_mm=3.0,
        iop=[1, 0, 0, 0, 1, 0], orientation_reliable=True,
    )
    check('spacing order is (col=x, row=y, slice=z)', geom.spacing_mm == (2.0, 1.5, 3.0), geom.spacing_mm)

    # round-trip: to_world then to_voxel should recover the original index
    for (k, j, i) in [(0, 0, 0), (10, 5, 7), (39, 59, 79)]:
        x, y, z = geom.to_world(k, j, i)
        rk, rj, ri = geom.to_voxel(x, y, z)
        check(f'to_voxel(to_world({k},{j},{i})) round-trips',
              abs(rk - k) < 1e-6 and abs(rj - j) < 1e-6 and abs(ri - i) < 1e-6,
              (rk, rj, ri))

    # explicit expected value at the origin voxel
    x0, y0, z0 = geom.to_world(0, 0, 0)
    check('to_world(0,0,0) equals origin_mm', (x0, y0, z0) == (10.0, -20.0, 5.0), (x0, y0, z0))

    # one step in each axis moves by exactly that axis's spacing
    x1, y1, _ = geom.to_world(0, 0, 1)
    check('one column step moves by col spacing (x)', abs((x1 - x0) - 2.0) < 1e-9, x1 - x0)
    _, y2, _ = geom.to_world(0, 1, 0)
    check('one row step moves by row spacing (y)', abs((y2 - y0) - 1.5) < 1e-9, y2 - y0)
    _, _, z3 = geom.to_world(1, 0, 0)
    check('one slice step moves by slice spacing (z)', abs((z3 - z0) - 3.0) < 1e-9, z3 - z0)

    k, j, i = world_to_voxel_clamped(geom, -9999, -9999, -9999)
    check('world_to_voxel_clamped clamps out-of-range points into the volume',
          k == 0 and j == 0 and i == 0, (k, j, i))
    k, j, i = world_to_voxel_clamped(geom, 9999, 9999, 9999)
    check('world_to_voxel_clamped clamps the far corner too',
          k == shape[0] - 1 and j == shape[1] - 1 and i == shape[2] - 1, (k, j, i))

    # --- unreliable orientation falls back to identity axes, not fabricated geometry ---
    fallback_geom = build_volume_geometry(
        shape=shape, origin_mm=None, pixel_spacing_row_col=[1.0, 1.0],
        slice_spacing_mm=2.0, iop=None, orientation_reliable=False,
    )
    check('fallback geometry is flagged unreliable', fallback_geom.orientation_reliable is False)
    check('fallback geometry still preserves physical spacing', fallback_geom.spacing_mm == (1.0, 1.0, 2.0))

    # --- marching cubes on a synthetic sphere mask ---
    iso_geom = build_volume_geometry(
        shape=(40, 40, 40), origin_mm=(0.0, 0.0, 0.0), pixel_spacing_row_col=[1.0, 1.0],
        slice_spacing_mm=1.0, iop=[1, 0, 0, 0, 1, 0], orientation_reliable=True,
    )
    mask = sphere_mask(shape=(40, 40, 40), radius=12)
    mesh = build_lung_mesh(mask, iso_geom, quality=QUALITY_HIGH_FIDELITY)
    check('high-fidelity mesh has vertices', mesh.vertices.shape[0] > 0)
    check('high-fidelity mesh has triangles', mesh.faces.shape[0] > 0)
    check('high-fidelity mesh vertices are 3D', mesh.vertices.shape[1] == 3)
    check('high-fidelity mesh face indices are in range',
          mesh.faces.max() < mesh.vertices.shape[0] and mesh.faces.min() >= 0)
    check('high-fidelity mesh normals match vertex count', mesh.normals.shape[0] == mesh.vertices.shape[0])
    check('high-fidelity mesh has no downsampling', mesh.downsample_factor == 1)

    # bounding box should approximate a sphere of ~radius 12mm (isotropic 1mm spacing)
    extent = mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0)
    check('mesh bounding box approximates the sphere diameter (~24mm)',
          all(20.0 < e < 28.0 for e in extent), extent)

    # --- interactive (downsampled) mesh on a larger volume ---
    big_mask = sphere_mask(shape=(200, 200, 200), radius=60)
    big_geom = build_volume_geometry(
        shape=(200, 200, 200), origin_mm=(0.0, 0.0, 0.0), pixel_spacing_row_col=[1.0, 1.0],
        slice_spacing_mm=1.0, iop=[1, 0, 0, 0, 1, 0], orientation_reliable=True,
    )
    interactive_mesh = build_lung_mesh(big_mask, big_geom, quality=QUALITY_INTERACTIVE)
    check('interactive mesh downsamples a large volume', interactive_mesh.downsample_factor > 1,
          interactive_mesh.downsample_factor)
    check('interactive mesh warns about downsampling', len(interactive_mesh.warnings) > 0)
    high_fidelity_mesh = build_lung_mesh(big_mask, big_geom, quality=QUALITY_HIGH_FIDELITY)
    check('high-fidelity mesh of the same volume has more vertices than interactive',
          high_fidelity_mesh.vertices.shape[0] > interactive_mesh.vertices.shape[0],
          (high_fidelity_mesh.vertices.shape[0], interactive_mesh.vertices.shape[0]))

    # --- error handling ---
    try:
        build_lung_mesh(np.zeros((20, 20, 20), dtype=bool), iso_geom, quality=QUALITY_HIGH_FIDELITY)
        check('empty mask raises MeshReconstructionError', False)
    except MeshReconstructionError:
        check('empty mask raises MeshReconstructionError', True)

    try:
        build_lung_mesh(np.ones((20, 20, 20), dtype=bool), iso_geom, quality=QUALITY_HIGH_FIDELITY)
        check('full mask raises MeshReconstructionError', False)
    except MeshReconstructionError:
        check('full mask raises MeshReconstructionError', True)

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
