# Next Tasks (Implementation Task Breakdown)

Implement one task at a time, in order. Do not attempt multiple tasks in a
single change. Every task must respect `structure.txt` (single backend file,
multi-page templates, no external medical/AI data transmission) and must
**not** add anything from the exclusion list at the bottom of this file.

---

## Task 1 — DICOM multi-file upload foundation
**Objective:** Accept a multi-file DICOM series upload from the frontend.
**Files expected to change:** `api/index.py`, `templates/dashboard.html`
**Requirements:** Multi-file selection, drag-and-drop target, file count
display, upload status display. Store uploads only in the gitignored
`uploads/` staging directory.
**Tests/checks:** Upload of a valid synthetic series; upload of zero files;
upload of a very large file count.
**Acceptance criteria:** Files reach the backend and are staged; UI reflects
count and status.
**Explicit exclusions:** No validation logic yet (Task 2), no reconstruction.

## Task 2 — DICOM validation
**Objective:** Implement `validate_dicom_series()` per `DICOM_PIPELINE.md`.
**Files expected to change:** `api/index.py`
**Requirements:** All checks listed in `DICOM_PIPELINE.md`, returning
PASS/WARNING/FAIL/NOT AVAILABLE per check.
**Tests/checks:** Corrupted DICOM, non-DICOM input, inconsistent
Rows/Columns, missing pixel data, duplicate `SOPInstanceUID`.
**Acceptance criteria:** Structural failures block progression; warnings do
not.
**Explicit exclusions:** No ordering or reconstruction logic yet.

## Task 3 — Spatial slice ordering
**Objective:** Implement `order_slices_spatially()` per `DICOM_PIPELINE.md`.
**Files expected to change:** `api/index.py`
**Requirements:** Slice-normal-based ordering; documented fallback to
`SliceLocation`/`InstanceNumber` with a `fallback_ordering_used` flag.
**Tests/checks:** Reversed input order, randomized filenames, duplicate
slice positions, irregular spacing, missing `ImagePositionPatient`, missing
`InstanceNumber`.
**Acceptance criteria:** Correct physical order regardless of filename or
input order; fallback usage is flagged.
**Explicit exclusions:** No HU conversion yet.

## Task 4 — HU conversion + volume construction
**Objective:** Implement `convert_to_hu()` and `build_volume()`.
**Files expected to change:** `api/index.py`, `requirements.txt` (if a new
numeric dependency is genuinely needed beyond numpy/pydicom)
**Requirements:** `[slice, row, column]` volume; per-slice slope/intercept,
never assumed constant; `NOT AVAILABLE` handling when slope/intercept missing.
**Tests/checks:** Varying slope/intercept across slices; missing
slope/intercept on some slices.
**Acceptance criteria:** Correct HU values on synthetic fixtures with known
expected output.
**Explicit exclusions:** No windowing/rendering yet.

## Task 5 — Study summary
**Objective:** Implement `build_study_summary()` and its API endpoint.
**Files expected to change:** `api/index.py`
**Requirements:** Return only the fields listed under "Study summary" in
`DICOM_PIPELINE.md`. No patient-identifying fields.
**Tests/checks:** Confirm no patient-identifying keys appear in the response
for a synthetic fixture that includes them in the source DICOM.
**Acceptance criteria:** Summary matches the documented schema exactly.
**Explicit exclusions:** No viewer rendering yet.

## Task 6 — Axial viewer
**Objective:** Render the axial plane of the reconstructed volume with slice
scrolling/slider.
**Files expected to change:** `api/index.py`, `templates/dashboard.html`
**Requirements:** Mouse-wheel + slider scrolling; current/total slice
readout.
**Tests/checks:** Axial coordinate mapping correctness.
**Acceptance criteria:** Correct slice displayed for a given index.
**Explicit exclusions:** No coronal/sagittal, no crosshairs, no windowing yet.

## Task 7 — Coronal + sagittal reconstruction
**Objective:** Derive coronal and sagittal views from the same volume.
**Files expected to change:** `api/index.py`, `templates/dashboard.html`
**Requirements:** All three views represent the same underlying volume.
**Tests/checks:** Coronal coordinate mapping; sagittal coordinate mapping.
**Acceptance criteria:** Consistent anatomy across all three planes for a
synthetic fixture with known geometry.
**Explicit exclusions:** No crosshair sync yet.

## Task 8 — Crosshair synchronization
**Objective:** Selecting a point in one view updates the other two.
**Files expected to change:** `templates/dashboard.html`, `api/index.py`
(if coordinate mapping needs backend support)
**Requirements:** Display current X/Y/Z voxel coordinates.
**Tests/checks:** Crosshair synchronization across all three planes.
**Acceptance criteria:** Coordinates match across views for the same
physical point.
**Explicit exclusions:** No windowing/HU inspection yet.

## Task 9 — Window/level controls + HU inspection
**Objective:** Implement WW/WL controls with Lung/Soft tissue/Bone presets,
and voxel HU inspection.
**Files expected to change:** `api/index.py`, `templates/dashboard.html`
**Requirements:** Non-destructive windowing (display-only). HU display, with
explicit "unavailable" state when conversion wasn't possible.
**Tests/checks:** Window/level calculations; HU display correctness and
unavailable-state handling.
**Acceptance criteria:** Underlying volume is unchanged after windowing;
presets match `CT_VIEWER_SPEC.md`.
**Explicit exclusions:** No segmentation/overlay controls beyond disabled
placeholders.

## Task 10 — Viewer performance and polish
**Objective:** Zoom, pan, reset view, maximize viewport, responsive layout,
and disabled placeholders for future segmentation/overlay features.
**Files expected to change:** `templates/dashboard.html`, `api/index.py`
**Requirements:** Reset view restores defaults without touching volume data.
Future-feature placeholders are visibly disabled and labeled "Not yet
implemented."
**Tests/checks:** Reset-view behavior; study-loading failure handling.
**Acceptance criteria:** Viewer is usable end-to-end for a synthetic study.
**Explicit exclusions:** Everything in the exclusion list below.

---

## Explicit exclusions (apply to all tasks above)

Disease classification, IPF prediction, fibrotic HP prediction, lung
segmentation, lobe segmentation, honeycombing detection, ground-glass
detection, machine-learning models, PyTorch, MONAI, LLM analysis,
radiology-report analysis, treatment recommendations, clinical decision
support. Do not claim HIPAA compliance, FDA approval, clinical validation, or
diagnostic capability anywhere.
