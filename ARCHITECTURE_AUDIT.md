# Architecture Audit

Date: 2026-08-29
Scope: full repository, as it exists on disk right now — not the aspirational
target. Every claim below was checked against the actual source (line numbers
given where useful) or against a real test run, not inferred from file names.

Baseline test run at time of writing (`./.venv/bin/python tests/run_all.py`,
after installing `requirements.txt` + `tests/requirements-test.txt` into the
venv, which were missing on this machine): **all 10 suites pass, 567 tests,
0 failures.**

---

## 1. What currently exists

**Backend.** Flask, single deployable entry point `api/index.py` (~1,420
lines), deployed to Vercel as one serverless Python function
(`vercel.json`). Supporting modules under `api/` contain no routes — pure
logic only, imported by `index.py`:

| Module | Responsibility |
|---|---|
| `db.py` | stdlib `sqlite3` schema + connection (doctors, cases, case_access, notes, audit_log) |
| `auth.py` | password hashing, session cookie, the single case-authorization chokepoint |
| `cases.py` | case/note CRUD (no authorization logic — that's `auth.py`'s job only) |
| `study_store.py` | durable on-disk study storage: `.npy` volumes (mmap'd on read), bit-packed segmentation masks, JSON meta/analysis, LRU memory cache |
| `mesh_reconstruction.py` | `VolumeGeometry` (voxel↔patient-mm affine), marching-cubes surface generation |
| `lung_segmentation.py` | rule-based (thresholding + 3D connected components) lung extraction, correctly labeled as not-AI |
| `quantitative_analysis.py` | scan quality, lung/per-side volumes, HU stats/percentiles, histograms, zones, pleural-distance bands, asymmetry |
| `density_regions.py` | cited densitometry (LAA%-950/-910/-856, HAA%-600/-250, Perc15) + 3D region clustering with morphology/fill-fraction |
| `overlay_render.py` | HU-band colour overlay rendering + base-to-apex composition profile |

**Frontend.** Multi-page templates (`templates/*.html`), split cleanly into
a cinematic public site (no auth, no clinical data) and a dense clinical
workspace (`cases.html`, `case_workspace.html`, `dashboard.html`,
`viewer.html`). `viewer.html` runs a Three.js 3D surface + a 2D
axial/coronal/sagittal panel with the density overlay wired in.

**Persistence.** SQLite file for structured data; per-study directories
under `instance/studies/<uuid>/` (volume.npy, meta.json, segmentation.npz,
analysis.json) for imaging data. Both are local disk.

**Auth/authorization.** Session-cookie identity (doctor id only, re-resolved
against the DB on every request — `auth.py:97`); a single explicit
`case_access` grant table is the only path that decides visibility
(`auth.py:142`); constant-time-ish login (dummy hash compare on unknown
email, `auth.py:69`); `SESSION_COOKIE_HTTPONLY`/`SameSite=Lax`/`Secure`
configured in `configure_session_cookie`.

**Audit log.** More complete than a first glance suggests — 15 distinct
event types are actually recorded from `index.py`, including
`case_access_denied`, `study_access_denied`, `imaging_study_imported`,
`analysis_generated`, `note_created`, `case_status_changed` — not just
login/logout.

**DICOM pipeline** (`index.py`): `validate_dicom_series`,
`order_slices_spatially`, `build_volume`, `convert_to_hu`,
`build_study_summary`. Confirmed by reading the code, not assumed:

- Slice ordering projects `ImagePositionPatient` onto the slice normal
  derived from `ImageOrientationPatient` (`order_slices_spatially`,
  `index.py:573`) — it does **not** sort by filename. Falls back to
  `SliceLocation`/`InstanceNumber` only when geometry tags are missing, and
  flags that fallback explicitly.
- HU conversion applies **per-slice** `RescaleSlope`/`RescaleIntercept`
  (`convert_to_hu`, `index.py:644`) rather than assuming a constant value
  across the series.
- `VolumeGeometry` is the single affine transform used consistently by
  segmentation, mesh reconstruction, and quantitative analysis — there is
  no second, divergent coordinate system anywhere in the codebase.

**Segmentation quality gates already implemented**
(`lung_segmentation.py`): body mask from the largest connected
soft-tissue component (excludes the scanner table — this was today's bug
fix), `BODY_EROSION_MM` skin-shell erosion, `LATERAL_SEPARATION_MIN` and
`SECOND_LUNG_MIN_RATIO` guards against mislabeling non-lateralised regions
as left/right lungs.

**Scientific-honesty discipline.** This is the most consistently-executed
part of the whole codebase. Every module that can't produce a real number
returns a structured `NOT_AVAILABLE`/`NOT_YET_ANALYZED` with a `reason`
field instead of a placeholder value; every derived metric carries a
`provenance` dict (`source`, `method`, `method_version`); nothing is
labeled "AI" that isn't; `density_regions.py`/`overlay_render.py` both
carry explicit module-level documentation of what they are *not*
(a validated ILD pattern classifier) precisely so a future reader doesn't
misread a density band as a diagnosis.

**Tests.** 567 tests, synthetic-fixture-based, ground-truth-by-construction
style (exact voxel counts / HU values / centroids, not just "doesn't
crash"). No real patient data in the repo (correctly gitignored:
`instance/`, `*.db`, `uploads/`, `*.dcm`).

---

## 2. What is technically sound — keep as-is

- The affine coordinate system (Section 9 of the target spec is
  substantially **already met** at the code level: voxel→patient-mm is one
  transform, used everywhere, never approximated).
- Spatial DICOM slice ordering and per-slice HU conversion (Sections 8/10 —
  already correct, already tested against reversed/randomized input order
  per `NEXT_TASKS.md` Task 3's acceptance criteria).
- The single-chokepoint authorization model and audit logging (Section 42 —
  already close to the target; no route bypasses `doctor_can_access_case`).
- The refusal discipline around fabricated findings (Section 43 — the
  strongest-executed section already; nothing needs walking back).
- The `NEXT_TASKS.md`/`structure.txt` engineering discipline itself (one
  task at a time, explicit exclusion lists, synthetic-only fixtures) — this
  process is *why* the honesty discipline above actually holds up under
  inspection. Don't discard it for a more conventional "ticket board" style.
- Segmentation plausibility gates already exist in a real, tested form
  (Section 13 is partially done, not a blank slate).

## 3. What is weak

- **No ORM, no migrations.** `db.py` is `CREATE TABLE IF NOT EXISTS` run at
  connection time. Any schema change today is a manual `ALTER TABLE` or a
  drop/recreate (`reset_db`, test-only). There is no way to know what
  version of the schema a given database file is at.
- **No processing-job system.** Segmentation, mesh generation, and
  densitometry all run synchronously inside the Flask request handler. A
  large study's `/reconstruct3d` or `/analysis` call blocks the worker for
  the full computation. There is no `ProcessingJob` record, no
  QUEUED/RUNNING/COMPLETED/FAILED state machine, nothing async.
- **No hash/version-keyed cache.** `study_store.py` persists results so
  they survive a restart, but nothing checks "does a valid result already
  exist for this (source hash, method version, parameters) combination"
  before recomputing. The content-hash de-duplication used to clean up the
  five imported studies was a one-off `/tmp` script, not integrated.
- **Single study per case.** `cases` has one nullable `study_id` column —
  a 1:1 relationship, not the 1:many `Case → ImagingStudy[]` the
  longitudinal-comparison target architecture needs. There is no
  `ImagingStudy`/`ImagingSeries` table at all; a "study" today is a
  filesystem directory keyed by a bare UUID, with no relational row.
- **No ROI/Measurement/Annotation persistence.** A clinician cannot place a
  point, draw a distance, or save a note anchored to image coordinates.
  `RegionOfInterest` currently exists only as an in-memory, non-persisted
  by-product of `density_regions.py`'s clustering — it disappears when the
  analysis isn't re-fetched, and has no clinician-editable counterpart.
- **No lobe segmentation, airway, vessel, or radiomics modules.** Not
  faked — genuinely absent, correctly reported as `NOT_AVAILABLE` wherever
  `quantitative_analysis.py` would otherwise report them.
- **No dataset adapter layer.** No OSIC or other adapter exists yet; there
  is no dataset-import architecture distinct from the doctor-driven upload
  flow.
- **No registration / longitudinal comparison architecture** — no
  `Registration` records, no "current vs. prior" comparison of any kind.
- **Provenance is per-module, not unified.** Each analysis module attaches
  its own `provenance` dict, which is good, but there's no
  `ProvenanceRecord` table joining every derived asset back to a source
  hash + method version in one place, and no versioning scheme for
  re-running an algorithm at a new version (a re-analysis today overwrites
  `analysis.json` in place — there is no "Analysis v1 / v2" retained
  side-by-side).
- **3D volume rendering claim needs verification, not trust.** The comment
  in `structure.txt` says viewer.html does "GPU volume raycasting" via
  Three.js; I have not yet opened this in a browser this session to confirm
  fidelity, transfer-function correctness, or whether it's closer to a
  lightweight approximation than a true ray-marched volume renderer. This
  needs an actual look before being counted as done.

## 4. The one architectural fork that blocks Phase 1

`db.py` and `study_store.py` both **already document, in their own
docstrings, that they are broken on the actual deployment target**: Vercel
serverless functions have an ephemeral, per-instance filesystem. A SQLite
file or `.npy` study directory written by one invocation is not guaranteed
visible to the next. This has apparently been tolerated so far because
local development and (presumably) low-traffic single-instance usage don't
surface it, but it is a real, previously-acknowledged, unresolved gap — not
something this audit is discovering for the first time.

The master spec's Phase 1 (Postgres, private object storage, a background
job worker) **cannot be half-done on Vercel serverless**: there's no
long-running worker process available on that platform for jobs, and
"private medical asset storage" on ephemeral local disk is a contradiction
in terms regardless of which database sits in front of it.

This is a genuine fork, not a style preference, and it changes cost,
deployment complexity, and how much of Sections 38-39 (job system, cache)
can be built at all:

- **A.** Stay on Vercel, add managed Postgres (Neon/Supabase/Vercel
  Postgres) + managed object storage (S3/R2) reachable over the network,
  and run background jobs via a hosted queue (e.g. a separate worker
  service, or synchronous-with-timeout as today but backed by shared
  storage so at least correctness survives instance churn). Job execution
  itself still can't run *inside* the Vercel function past its request
  lifetime.
- **B.** Move off serverless to a long-running process (Fly.io, Render,
  a VM) where local disk becomes viable again, a real background worker
  (RQ/Celery/APScheduler) can run in-process, and Postgres can even be
  self-hosted alongside it if wanted.

I'm not picking this — it's a cost/ops decision, not a technical one, and
it determines what Phase 1 actually looks like.

## 5. Proposed target architecture (delta from current)

```
Case (1) ──< ImagingStudy (N) ──< ImagingSeries (N)
                 │                      │
                 │                      └─ private asset storage ref (raw DICOM)
                 └─ CanonicalVolume (1 per series) ── asset ref (.npy / equivalent)
                          │
                          ├─ Segmentation (N, versioned) ── asset ref (mask)
                          │        └─ ReferenceMesh / InteractiveMesh — asset refs
                          ├─ QuantitativeAnalysis (N, versioned)
                          ├─ RegionOfInterest (N) ── Measurement / Annotation
                          └─ ProcessingJob (N) — tracks async work on any of the above
```

Everything under "asset storage ref" is a UUID + hash + storage key in the
relational DB; the bytes themselves live in object storage (or local disk
in dev), never in a DB column, matching Section 3 of the spec and what
`study_store.py` already does in spirit — it just needs a relational row
in front of it instead of being the source of truth by itself.

## 6. Migration risks

- **Existing 9 study directories under `instance/studies/`** and the
  `vitalitysync.db` file are real accumulated state from this session's
  work (5 unique re-processed studies + duplicates). Any migration must
  read these forward into the new schema, not discard them — they're the
  only real-data verification evidence this project has.
- **`study_store.py`'s dict-like API** (`get`/`pop`/`in`/`__setitem__`) is
  relied on by `index.py` and every existing test. Introducing a relational
  `ImagingStudy` row alongside it should wrap the existing store, not
  replace its on-disk format outright, to avoid a flag-day rewrite of every
  call site and test in one change.
- **`cases.study_id` is a live column** read/written by `cases.py` and
  `index.py`. Moving to `Case → ImagingStudy[]` needs a compatibility
  read path (or a data migration inserting one `ImagingStudy` row per
  existing `study_id`) so existing cases don't silently lose their linked
  study.
- **No Alembic yet** — the first migration tool introduction always carries
  the risk of drifting from what `SCHEMA` in `db.py` already created on
  existing dev/prod databases. The safe order is: snapshot current schema
  as migration 0, verify `alembic upgrade head` is a no-op against an
  existing `vitalitysync.db`, then proceed.

## 7. Implementation order recommendation

Following the master spec's own Section 46 ordering, adjusted for what's
already done:

1. **Decide the hosting fork (Section 4 above)** — blocks everything else
   in Phase 1.
2. **Phase 1, reduced scope**: introduce SQLAlchemy models + Alembic over
   the *existing* SQLite schema first (no Postgres cutover yet), add
   `ImagingStudy`/`ImagingSeries`/`ProcessingJob` tables that wrap the
   existing file-based study store rather than replacing it, add a
   hash+version-keyed cache check before recomputation.
3. **Phase 1b**: once the hosting fork is resolved, cut over to Postgres +
   object storage using the same models (SQLAlchemy makes this a connection
   string + a storage-backend swap, not a rewrite).
4. **Phase 4 pieces that don't need Phase 1**: ROI/Measurement persistence
   can be added against the *current* SQLite schema now, independent of
   the Postgres decision, since it's new tables, not a migration of
   existing ones. This is probably the highest-value next step that
   requires no infrastructure decision at all.
5. Everything else (lobes, airways, vessels, radiomics, dataset adapters,
   longitudinal/registration) per the spec's Section 46 ordering, gated on
   1-4 being real and tested, not scaffolded.

---

*This document reflects the codebase as read and the test suite as run
today. It will go stale the moment either changes — treat it as a snapshot,
not a living spec.*
