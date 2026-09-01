# CT Viewer Spec

Stage 1 imaging foundation only. Depends on the reconstructed volume described
in `DICOM_PIPELINE.md`. No segmentation or diagnostic overlays are implemented
here — see "Future controls" below for how those are represented as disabled
placeholders.

## Views

Three synchronized views of the same reconstructed volume:
- Axial
- Coronal
- Sagittal

## Interactions

- Mouse-wheel slice scrolling
- Slice slider with current slice / total slices readout
- Zoom
- Pan
- Reset view (returns zoom/pan/window/level to defaults, never touches the
  underlying volume data)
- Maximize a single viewport
- Responsive layout across viewport sizes

## Crosshair synchronization

Selecting a location in one plane updates the corresponding location in the
other two planes. The current voxel coordinates (X, Y, Z) are displayed
alongside the crosshair.

## Windowing (window width / window level)

Presets:

| Preset      | Window Width | Window Level |
|-------------|-------------:|-------------:|
| Lung        | ~1500 HU     | ~-600 HU     |
| Soft tissue | TBD (standard soft-tissue range) | TBD |
| Bone        | TBD (standard bone range) | TBD |

Windowing only changes how the current slice is *displayed* (a rendering
transform applied at request time); it never mutates the underlying HU/pixel
volume.

## Hounsfield-unit inspection

When a voxel is selected, show its HU value if HU conversion is reliably
available for that slice:

```
X: 215
Y: 183
Z: 121
HU: -824
```

If HU conversion is unavailable for that slice (see `DICOM_PIPELINE.md`), the
UI must say so explicitly rather than showing a fabricated or interpolated
value.

## Future controls (not implemented — visibly disabled placeholders only)

- Lung segmentation
- Lobe segmentation
- Abnormality overlay
- Finding heat maps

Each must be rendered as a clearly disabled/greyed-out control labeled "Not
yet implemented," never as a working feature.

## Tests to plan for

Axial/coronal/sagittal coordinate mapping, crosshair synchronization,
window/level calculation correctness, HU display (including the "unavailable"
state), and reset-view behavior.
