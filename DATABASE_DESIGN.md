# Database Design

Reflects what is actually implemented as of this change. Two separate
persistence layers exist side by side, deliberately — see "Why two layers"
below before assuming this should be unified.

## Layer 1: `api/db.py` (unchanged by this work)

Stdlib `sqlite3`, no ORM. Tables: `doctors`, `cases`, `case_access`, `notes`,
`audit_log`. Schema is `CREATE TABLE IF NOT EXISTS` run at connection time
(`db.init_db()`), not Alembic-managed. This is the working, tested
authentication/case-management system and was not touched — see
`ARCHITECTURE_AUDIT.md` section 2 for why rewriting it wasn't necessary to
add the capabilities below.

## Layer 2: `api/models.py` (new — the imaging-relational layer)

SQLAlchemy 2.0 declarative models, Alembic-migrated (`migrations/`).

```
ImagingStudy (1) ──< ImagingSeries (N)
     │
     ├──< ProcessingJob (N)
     ├──< RegionOfInterest (N)
     ├──< Measurement (N)
     └──< Annotation (N)
```

### ImagingStudy

One row per imported CT study. **`id` is the same UUID string used as the
key into `study_store.StudyStore`** — this table is the structured index
sitting in front of the file-based pixel-data store, not a replacement for
it (see `ARCHITECTURE_AUDIT.md` section 3: large arrays never belong in a
relational column).

`case_id` and `owner_doctor_id` are plain indexed integers referencing
`db.py`'s `cases.id` / `doctors.id` — **not** declared foreign keys, because
they live in a different database file today (see "Why two layers"). The
reference is enforced at the application layer, in `api/measurements.py`
and the route handlers in `api/index.py`, the same way `cases.study_id`
already references the file-based store with no DB-level FK.

`status` progresses `imported → segmented → analyzed` (or `failed`), set by
`api/index.py`'s route handlers as each pipeline stage actually completes.

### ImagingSeries

Technical acquisition metadata (slice count, pixel spacing, orientation
reliability, fallback-ordering flag, HU availability) — the same fields
`build_study_summary()` already computes, now given a relational home.
**The current ingestion pipeline creates exactly one series per study** —
the 1:many relationship is schema-ready for multi-series import, which is
not implemented.

### ProcessingJob

Records `job_type` (IMPORT/SEGMENTATION/MESH_GENERATION/QUANTITATIVE_ANALYSIS),
`status` (QUEUED/RUNNING/COMPLETED/FAILED), timing, method/version, and
error messages.

**Honest limitation, stated in the model's own docstring:** this deployment
has no background worker (see `ARCHITECTURE_AUDIT.md` section 4 — Vercel
serverless has no long-running process to dequeue work). Every job today is
created and completed synchronously inside the request that does the work.
QUEUED is set and immediately transitioned; it is never observed sitting in
that state. This table exists so timing/failure/version data is captured
uniformly *now*, and so a real queue can be dropped in later without a
schema change — not because jobs are asynchronous today.

### RegionOfInterest

A locatable region, `source` = `'clinician_annotation'` or
`'deterministic_segmentation'`. A clinician's `label` is free text; a
deterministic region (persisted from `density_regions.py`'s output via
`POST /regions/sync-deterministic`) **always has `label = null` and
`created_by_doctor_id = null`** — enforced in `measurements.create_region`,
not just by convention — because it is a measured density cluster, not a
diagnosis, and nothing should ever make it look clinician-authored or
disease-named.

### Measurement

`measurement_type`, `geometry_json` (patient-space millimetre points),
`value`, `units`. Only `point_hu` and `distance` are computable by the
current API — see `QUANTITATIVE_ANALYSIS.md` for why `area`/`volume`/the
diameter types are valid schema values with no endpoint yet. Geometry is
always resolved server-side from the study's own affine transform
(`VolumeGeometry.to_world`, in `api/mesh_reconstruction.py`) from client-sent
**voxel indices** — never trusted as client-computed millimetres, and never
derived from screen pixels.

### Annotation

Free-text note, optionally anchored to a patient-space point via the same
voxel→mm resolution as `Measurement`.

## Why two layers (not one Postgres migration today)

`ARCHITECTURE_AUDIT.md` section 4 records the user's decision: stay on
Vercel, add managed Postgres + object storage. That is a real infrastructure
change (provisioning a database, updating environment configuration) that
hadn't happened yet as of this change. Building the *new* tables against
SQLAlchemy from day one means that decision, once actioned, is a
`IMAGING_DATABASE_URL` connection-string change plus `alembic upgrade head`
— not a rewrite.

For **local SQLite development only**, `api/db_engine.py` defaults to a
second file (`instance/imaging.db`), separate from `db.py`'s
`vitalitysync.db`, to avoid two different SQLite drivers (`sqlite3` and
SQLAlchemy's own pool) contending for locks on one file under concurrent
writes. This split is transitional: once `IMAGING_DATABASE_URL` points at a
real Postgres instance, `db.py`'s tables could move into the same database
in a later, separate change.

## Migrations

```
alembic upgrade head      # apply all pending migrations
alembic revision --autogenerate -m "..."   # after changing api/models.py
```

`migrations/env.py` reads `IMAGING_DATABASE_URL` (same env var the app
uses), so the same migration set runs against local SQLite and a real
Postgres instance without editing `alembic.ini` per environment.

`db.py`'s tables are **not** part of this migration set. Schema changes
there remain a manual edit to `db.py`'s `SCHEMA` string, as before this
change — unifying that is future work, not done here (see
`ARCHITECTURE_AUDIT.md` section 7).

## What was verified, not just written

- `alembic upgrade head` applied cleanly against a fresh SQLite target
  (checked by inspecting `sqlite_master` afterward).
- 25 tests in `tests/test_models.py`: round-trip persistence for every
  table, cascade delete from `ImagingStudy` to every child table, rejection
  of a labeled deterministic region, rejection of an unknown job type,
  ownership-checked delete on regions/measurements/annotations.
- 38 integration tests in `tests/test_measurements_api.py` against the real
  Flask app + synthetic DICOM fixtures: a `point_hu` measurement's value
  matches the existing `/voxel` endpoint's HU reading at the same
  coordinate; a `distance` measurement across 10 voxel columns matches
  `10 × spacing_mm[0]` from the study's own `/geometry` endpoint (i.e.,
  checked against real physical spacing, not merely "did not crash"); a
  second doctor gets 404 (not 403) attempting to read another doctor's
  study measurements, matching the existing anti-enumeration convention in
  `_get_owned_study_or_error`.
- Full existing suite (567 tests, 10 suites) re-run after every change in
  this session with zero regressions.
