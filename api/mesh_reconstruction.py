"""
3D reconstruction module: turns a binary segmentation mask + physical DICOM
voxel geometry into a triangulated surface mesh (marching cubes), and defines
the single source of truth for the coordinate mapping used everywhere the 3D
viewer talks to the 2D CT viewer.

COORDINATE SYSTEM (read this before touching any transform math)
------------------------------------------------------------------
Four coordinate spaces are in play, and this module documents/implements the
mapping between all of them:

  1. DICOM patient space (LPS, millimeters) - the real-world coordinate
     system from ImagePositionPatient / ImageOrientationPatient. +X = patient
     Left, +Y = patient Posterior, +Z = patient Superior.
  2. Volume index space (k = slice, j = row, i = column) - integer indices
     into the reconstructed `hu_volume` array, in the spatially-sorted order
     produced by `order_slices_spatially` in api/index.py.
  3. Mesh space - marching-cubes output vertices, expressed directly in
     patient-space millimeters (this module bakes the volume->patient
     transform into the mesh at generation time, so mesh vertices ARE
     patient-space coordinates; there is no separate "mesh space").
  4. Browser/viewer space - Three.js scene units. The frontend treats 1 unit
     == 1 mm and applies no additional scaling, so viewer space is patient
     space translated only by a display-centering offset it tracks itself.

VolumeGeometry.to_world(k, j, i) and .to_voxel(x, y, z) are the single
implementation of the volume<->patient-space affine transform. Every
endpoint that needs this mapping (mesh generation, volume-texture geometry,
2D<->3D coordinate sync) is expected to use this class rather than
re-deriving the math.

Fidelity note: this transform assumes an axis-aligned-per-slice acquisition
(the row/column direction cosines are honored exactly; slice spacing is
approximated by the study's median inter-slice spacing, matching the
approximation already used for `slice_spacing_mm` in the study summary). When
`orientation_reliable` is False (fallback slice ordering was used, or
ImageOrientationPatient/ImagePositionPatient were unavailable), the origin
and axes fall back to an identity frame: physical proportions and spacing
are still respected, but absolute patient-space orientation/position is not
implied and left/right labeling is skipped upstream in `lung_segmentation.py`.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from scipy import ndimage
from skimage import measure


class MeshReconstructionError(Exception):
    pass


@dataclass
class VolumeGeometry:
    origin_mm: Tuple[float, float, float]        # ImagePositionPatient of ordered slice 0, or (0,0,0)
    spacing_mm: Tuple[float, float, float]       # (x=col spacing, y=row spacing, z=slice spacing)
    col_cosines: Tuple[float, float, float]      # world direction per +1 column index (i)
    row_cosines: Tuple[float, float, float]      # world direction per +1 row index (j)
    slice_cosines: Tuple[float, float, float]    # world direction per +1 slice index (k)
    orientation_reliable: bool
    shape: Tuple[int, int, int]                  # (n_slices, rows, cols)

    def to_world(self, k, j, i):
        """Volume index (k=slice, j=row, i=col) -> patient-space (x, y, z) mm."""
        origin = np.array(self.origin_mm, dtype=np.float64)
        sx, sy, sz = self.spacing_mm
        col = np.array(self.col_cosines, dtype=np.float64)
        row = np.array(self.row_cosines, dtype=np.float64)
        nrm = np.array(self.slice_cosines, dtype=np.float64)
        p = origin + (i * sx) * col + (j * sy) * row + (k * sz) * nrm
        return tuple(p)

    def to_voxel(self, x, y, z):
        """Patient-space (x, y, z) mm -> fractional volume index (k, j, i)."""
        origin = np.array(self.origin_mm, dtype=np.float64)
        sx, sy, sz = self.spacing_mm
        d = np.array([x, y, z], dtype=np.float64) - origin
        col = np.array(self.col_cosines, dtype=np.float64)
        row = np.array(self.row_cosines, dtype=np.float64)
        nrm = np.array(self.slice_cosines, dtype=np.float64)
        i = float(np.dot(d, col)) / sx
        j = float(np.dot(d, row)) / sy
        k = float(np.dot(d, nrm)) / sz
        return (k, j, i)

    def to_public_dict(self):
        return {
            "origin_mm": list(self.origin_mm),
            "spacing_mm": list(self.spacing_mm),
            "col_cosines": list(self.col_cosines),
            "row_cosines": list(self.row_cosines),
            "slice_cosines": list(self.slice_cosines),
            "orientation_reliable": self.orientation_reliable,
            "shape": {"slices": self.shape[0], "rows": self.shape[1], "cols": self.shape[2]},
        }


def build_volume_geometry(shape, origin_mm, pixel_spacing_row_col, slice_spacing_mm,
                           iop, orientation_reliable):
    """
    Args:
        shape: (n_slices, rows, cols)
        origin_mm: ImagePositionPatient of ordered slice 0, (x, y, z), or None
        pixel_spacing_row_col: DICOM PixelSpacing = [row_spacing, col_spacing], or None
        slice_spacing_mm: median inter-slice spacing (mm), or None
        iop: ImageOrientationPatient (6 floats), or None
        orientation_reliable: bool - whether iop/origin came from real, non-fallback geometry
    """
    if orientation_reliable and iop is not None and len(iop) == 6:
        col_cosines = tuple(float(v) for v in iop[0:3])
        row_cosines = tuple(float(v) for v in iop[3:6])
        normal = np.cross(np.array(col_cosines), np.array(row_cosines))
        norm = np.linalg.norm(normal)
        slice_cosines = tuple(float(v) for v in (normal / norm if norm > 1e-8 else np.array([0.0, 0.0, 1.0])))
    else:
        col_cosines = (1.0, 0.0, 0.0)
        row_cosines = (0.0, 1.0, 0.0)
        slice_cosines = (0.0, 0.0, 1.0)
        orientation_reliable = False

    origin = tuple(float(v) for v in origin_mm) if (orientation_reliable and origin_mm is not None) else (0.0, 0.0, 0.0)
    row_sp, col_sp = (pixel_spacing_row_col or [1.0, 1.0])[:2]
    spacing = (float(col_sp), float(row_sp), float(slice_spacing_mm) if slice_spacing_mm else 1.0)

    return VolumeGeometry(
        origin_mm=origin, spacing_mm=spacing, col_cosines=col_cosines, row_cosines=row_cosines,
        slice_cosines=slice_cosines, orientation_reliable=orientation_reliable, shape=tuple(shape),
    )


# ---------------------------------------------------------------------------
# Mesh generation
# ---------------------------------------------------------------------------

QUALITY_INTERACTIVE = "interactive"
QUALITY_HIGH_FIDELITY = "high_fidelity"
INTERACTIVE_MAX_DIM = 160  # display-only downsample cap for the interactive mesh


@dataclass
class MeshResult:
    vertices: np.ndarray   # (N, 3) float32, patient-space mm
    faces: np.ndarray      # (M, 3) int32
    normals: np.ndarray    # (N, 3) float32
    quality: str
    downsample_factor: int
    warnings: list


def build_lung_mesh(mask, geometry: VolumeGeometry, quality=QUALITY_HIGH_FIDELITY):
    """
    Marching-cubes surface reconstruction of `mask`, honoring true physical
    voxel spacing, with vertices returned directly in patient-space mm.

    `quality=interactive` downsamples the MASK (nearest-neighbor, integer
    factor) before marching cubes, bounding mesh complexity for smooth
    real-time interaction. This is a separate, clearly-labeled representation
    - it never overwrites or is confused with the high-fidelity mesh, and the
    underlying segmentation mask stored server-side is never modified.

    `quality=high_fidelity` runs marching cubes at the mask's native
    resolution (the same resolution as the reconstructed HU volume) with no
    downsampling - this is the scientific-reference representation.
    """
    warnings = []
    working_mask = mask
    downsample_factor = 1

    if quality == QUALITY_INTERACTIVE:
        max_dim = max(mask.shape)
        if max_dim > INTERACTIVE_MAX_DIM:
            downsample_factor = int(np.ceil(max_dim / INTERACTIVE_MAX_DIM))
            working_mask = mask[::downsample_factor, ::downsample_factor, ::downsample_factor]
            warnings.append(
                f"Interactive mesh downsampled {downsample_factor}x from the native mask "
                f"resolution for real-time performance; switch to High Fidelity for the "
                f"scientific-reference reconstruction."
            )
    elif quality != QUALITY_HIGH_FIDELITY:
        raise MeshReconstructionError(f"Unknown quality level: {quality!r}")

    if working_mask.sum() == 0:
        raise MeshReconstructionError(
            "The mask is empty at this quality level; cannot run marching cubes."
        )
    if working_mask.sum() == working_mask.size:
        raise MeshReconstructionError(
            "The mask fills the entire volume; marching cubes has no surface to extract."
        )

    spacing = (
        geometry.spacing_mm[2] * downsample_factor,  # slice (k / axis0)
        geometry.spacing_mm[1] * downsample_factor,  # row   (j / axis1)
        geometry.spacing_mm[0] * downsample_factor,  # col   (i / axis2)
    )

    padded = np.pad(working_mask, 1, mode="constant", constant_values=0).astype(np.float32)
    try:
        verts, faces, normals, _values = measure.marching_cubes(padded, level=0.5, spacing=spacing)
    except (ValueError, RuntimeError) as exc:
        raise MeshReconstructionError(f"Marching cubes failed: {exc}") from exc

    # Undo the 1-voxel padding offset (in the downsampled index frame).
    verts = verts - np.array(spacing, dtype=np.float64)

    # Map from (axis0=slice, axis1=row, axis2=col) mm-offsets to patient-space,
    # using the same origin/axis basis as VolumeGeometry.to_world - this must
    # stay in lockstep with VolumeGeometry so mesh clicks resolve to the
    # correct CT slice.
    origin = np.array(geometry.origin_mm, dtype=np.float64)
    col = np.array(geometry.col_cosines, dtype=np.float64)
    row = np.array(geometry.row_cosines, dtype=np.float64)
    nrm = np.array(geometry.slice_cosines, dtype=np.float64)

    world_verts = (
        origin
        + verts[:, 0:1] * nrm
        + verts[:, 1:2] * row
        + verts[:, 2:3] * col
    )
    world_normals = (
        normals[:, 0:1] * nrm
        + normals[:, 1:2] * row
        + normals[:, 2:3] * col
    )
    norm_len = np.linalg.norm(world_normals, axis=1, keepdims=True)
    norm_len[norm_len < 1e-8] = 1.0
    world_normals = world_normals / norm_len

    return MeshResult(
        vertices=world_verts.astype(np.float32),
        faces=faces.astype(np.int32),
        normals=world_normals.astype(np.float32),
        quality=quality,
        downsample_factor=downsample_factor,
        warnings=warnings,
    )


def encode_typed_array(arr, dtype):
    """Base64-encode a numpy array as flat little-endian `dtype`, for compact
    JSON transfer (decoded client-side directly into a JS typed array via
    atob + Uint8Array, avoiding a per-number JSON parse)."""
    import base64
    return base64.b64encode(np.ascontiguousarray(arr, dtype=dtype).tobytes()).decode("ascii")


def world_to_voxel_clamped(geometry: VolumeGeometry, x, y, z):
    """to_voxel(), clamped and rounded to valid integer (k, j, i) indices."""
    k, j, i = geometry.to_voxel(x, y, z)
    n_slices, rows, cols = geometry.shape
    k = int(round(min(max(k, 0), n_slices - 1)))
    j = int(round(min(max(j, 0), rows - 1)))
    i = int(round(min(max(i, 0), cols - 1)))
    return k, j, i
