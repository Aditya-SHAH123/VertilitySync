# DICOM Pipeline Spec

Stage 1 imaging foundation only. No disease detection, segmentation, or diagnostic
logic belongs here. All logic lives in `api/index.py` per `structure.txt`.

## Flow

```
upload series → validate files → organize slices → reconstruct volume
→ convert to HU (when possible) → build study summary → open in viewer
```

## Validation

Run per-file and series-level checks. Each check reports one of:
`PASS`, `WARNING`, `FAIL`, `NOT AVAILABLE`.

Checks:
- File is readable as DICOM
- Modality is CT (when the field is present)
- `StudyInstanceUID` is consistent across all files
- `SeriesInstanceUID` is consistent across all files
- No duplicate `SOPInstanceUID`
- Slice count is sane (e.g. > 1)
- `Rows` / `Columns` are consistent across the series
- `PixelSpacing` present and consistent
- `ImageOrientationPatient` present
- `ImagePositionPatient` present
- `SliceThickness` present
- `RescaleSlope` / `RescaleIntercept` present (per slice — do not assume shared)
- `PhotometricInterpretation` recognized
- Pixel data present and decodable
- File is not corrupted / not an unsupported transfer syntax

A series can proceed to reconstruction with `WARNING`s, but any `FAIL` on
structural checks (unreadable file, inconsistent Rows/Columns, missing pixel
data) blocks reconstruction and is surfaced to the user with a clear message.

## Slice ordering

**Never sort by filename.**

Preferred method: compute the slice-normal vector from `ImageOrientationPatient`
(cross product of the row/column direction cosines), then project each slice's
`ImagePositionPatient` onto that normal and sort by the resulting scalar. This
is robust to reversed input order and randomized filenames.

Documented fallback (only when orientation/position metadata is unavailable):
1. `SliceLocation`
2. `InstanceNumber`

Whenever a fallback is used, the study summary must flag `fallback_ordering_used:
true` and name which field was used, so the frontend can show a warning.

Edge cases to handle explicitly:
- Duplicate slice positions → warn, keep first occurrence, flag duplicates
- Irregular spacing between slices → warn, report min/median/max spacing
- Large gaps between slices → warn
- Reversed input order → resolved naturally by spatial sort
- Randomized filenames → resolved naturally by spatial sort (filenames are
  never used for ordering)

## Volume construction

Internal representation: a 3D array indexed `[slice, row, column]`.

## Hounsfield unit conversion

```
HU = pixel_value * RescaleSlope + RescaleIntercept
```

Applied per slice — `RescaleSlope`/`RescaleIntercept` are read from each
dataset individually and are never assumed identical across the series. If
either value is missing for a slice, that slice's HU conversion status is
`NOT AVAILABLE` and raw pixel values are kept instead; the overall study
summary reports whether HU conversion is fully, partially, or not available.

## Study summary (returned to frontend)

- Volume dimensions (slices × rows × columns)
- Pixel spacing
- Slice spacing (and whether it's regular or irregular)
- Image orientation
- Slice positions (relative, not tied to patient-identifying data)
- HU conversion status (full / partial / unavailable)
- Non-identifying technical scanner metadata when available (e.g. manufacturer,
  modality, kVp, slice thickness)
- `fallback_ordering_used` flag

**Never included:** patient name, patient ID, birth date, accession number, or
any other patient-identifying field.

## Privacy & security

- Uploaded files live only in the gitignored `uploads/` staging area or in
  memory; never committed to git.
- Generated volumes/caches are gitignored.
- No DICOM pixel data or metadata is ever sent to an external service
  (including any LLM/AI API used elsewhere in this app).
- Test fixtures must be synthetic DICOM data, never real patient scans.
- No HIPAA compliance, FDA approval, or clinical validation is claimed
  anywhere in the app or its documentation.

## Tests to plan for

Spatial ordering (normal case, reversed input, randomized filenames),
duplicate `SOPInstanceUID`, duplicate slice positions, missing
`ImagePositionPatient`, missing `InstanceNumber`, corrupted DICOM, non-DICOM
input, inconsistent dimensions, inconsistent spacing, HU conversion with
varying slope/intercept per slice, and study-loading failure handling.
