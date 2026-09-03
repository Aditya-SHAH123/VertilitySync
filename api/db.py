"""
SQLite persistence for doctor accounts, cases, notes, and the audit log.

Uses the Python standard library `sqlite3` only - no ORM - to keep the
deployed serverless function small and avoid adding heavy dependencies to a
project that previously had no database at all.

DEPLOYMENT LIMITATION (documented, not worked around):
    On Vercel's serverless Python runtime the filesystem is ephemeral and not
    shared between function instances, so a SQLite file under /tmp lives only
    as long as one instance. Accounts/cases created on one invocation may not
    be visible on another. This mirrors the pre-existing limitation of the
    in-memory STUDIES dict in api/index.py. For a real multi-user deployment
    this module's connection target should be pointed at a managed database;
    the schema and queries here are deliberately plain SQL to make that
    swap straightforward.

Nothing in this module stores DICOM pixel data. Cases and imaging studies
reference reconstructed volumes by opaque study id only - the pixel data
itself lives in study_store.py's private on-disk store.

The `patients` table is the one deliberate exception to "no identifying
data": a patient record's whole purpose is to persist a doctor's own
demographic labels (name, DOB, sex, an internal ID they assign) across
repeated visits and scans, so scan history and notes stay attached to the
same person over time.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

# Default to a local file next to the repo for development; override with
# DATABASE_PATH (e.g. /tmp/vitalitysync.db on a serverless host).
DEFAULT_DB_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vitalitysync.db"),
)

_local = threading.local()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_db(path=None):
    """Per-thread connection. Flask's dev server and test client are threaded,
    and sqlite3 connections are not safe to share across threads."""
    db_path = path or DEFAULT_DB_PATH
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == db_path:
        return conn
    if conn is not None:
        conn.close()
    parent_dir = os.path.dirname(db_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise sqlite3.OperationalError(
            f"{exc} (resolved DATABASE_PATH={db_path!r}; "
            f"DATABASE_PATH env var={os.environ.get('DATABASE_PATH')!r})"
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _local.conn = conn
    _local.path = db_path
    return conn


def close_db():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    display_name   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    case_ref          TEXT NOT NULL UNIQUE,
    owner_doctor_id   INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'needs_review',
    is_demo           INTEGER NOT NULL DEFAULT 0,
    study_id          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Explicit per-doctor grants. Ownership alone is not consulted by the
-- authorization check; the owner is also inserted here at creation time so
-- there is exactly one code path for "may this doctor see this case".
CREATE TABLE IF NOT EXISTS case_access (
    case_id    INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    doctor_id  INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'viewer',
    granted_at TEXT NOT NULL,
    PRIMARY KEY (case_id, doctor_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id           INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    author_doctor_id  INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Security-relevant events only. Deliberately does NOT store note bodies,
-- imaging content, or any clinical detail - just who did what to which
-- opaque identifier, and when.
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    doctor_id    INTEGER,
    event        TEXT NOT NULL,
    target_type  TEXT,
    target_id    TEXT,
    outcome      TEXT NOT NULL DEFAULT 'success',
    ip           TEXT
);

-- ---------------------------------------------------------------------
-- Patient workflow: Doctor -> Patient -> (ImagingStudy | ClinicalNote)
-- A patient persists across visits: repeated scans and notes accumulate
-- against the same row rather than each upload being a one-off session.
-- Authorization is ownership-based (patients.doctor_id), matching the
-- simplicity already used for cases; see auth.doctor_can_access_patient.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS patients (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id              INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    first_name             TEXT NOT NULL,
    last_name              TEXT NOT NULL,
    date_of_birth          TEXT,
    sex                    TEXT,
    internal_patient_id    TEXT NOT NULL,
    medical_record_number  TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    archived               INTEGER NOT NULL DEFAULT 0,
    UNIQUE (doctor_id, internal_patient_id)
);

-- One row per unique imported study, indexing the file-based volume that
-- study_store.py actually holds (patient_studies.id == that study's uuid
-- key). This table exists so a patient's scan history is queryable and
-- durable without storing pixel data in SQLite - see ASSET_STORAGE.md /
-- DATABASE_DESIGN.md for the same principle applied to the separate
-- imaging-relational (SQLAlchemy) layer.
CREATE TABLE IF NOT EXISTS patient_studies (
    id                   TEXT PRIMARY KEY,
    patient_id           INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    doctor_id            INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    study_instance_uid   TEXT,
    series_instance_uid  TEXT,
    modality             TEXT,
    scan_date            TEXT,
    slice_count          INTEGER,
    scan_hash            TEXT NOT NULL,
    processing_status    TEXT NOT NULL DEFAULT 'ready',
    created_at           TEXT NOT NULL
);

-- Patient-level notes have study_id = NULL; scan-specific notes set it to
-- a patient_studies.id. original_content is never overwritten - only
-- current_content changes when a doctor accepts an AI rewrite.
CREATE TABLE IF NOT EXISTS clinical_notes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id         INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    patient_id        INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    study_id          TEXT REFERENCES patient_studies(id) ON DELETE CASCADE,
    title             TEXT,
    original_content  TEXT NOT NULL,
    current_content   TEXT NOT NULL,
    ai_rewritten      INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_owner ON cases(owner_doctor_id);
CREATE INDEX IF NOT EXISTS idx_case_access_doctor ON case_access(doctor_id);
CREATE INDEX IF NOT EXISTS idx_notes_case ON notes(case_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_patients_doctor ON patients(doctor_id);
CREATE INDEX IF NOT EXISTS idx_patient_studies_patient ON patient_studies(patient_id);
CREATE INDEX IF NOT EXISTS idx_patient_studies_hash ON patient_studies(scan_hash);
CREATE INDEX IF NOT EXISTS idx_patient_studies_uid ON patient_studies(study_instance_uid);
CREATE INDEX IF NOT EXISTS idx_clinical_notes_patient ON clinical_notes(patient_id);
CREATE INDEX IF NOT EXISTS idx_clinical_notes_study ON clinical_notes(study_id);
"""


def init_db(path=None):
    conn = get_db(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def reset_db(path=None):
    """Drops and recreates every table. Test-support only."""
    conn = get_db(path)
    for table in ("audit_log", "clinical_notes", "patient_studies", "patients",
                  "notes", "case_access", "cases", "doctors"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_audit(event, doctor_id=None, target_type=None, target_id=None,
                  outcome="success", ip=None, path=None):
    """Appends a security-relevant event.

    Callers must pass only non-sensitive identifiers. Never pass note text,
    patient metadata, or imaging content.
    """
    conn = get_db(path)
    conn.execute(
        "INSERT INTO audit_log (ts, doctor_id, event, target_type, target_id, outcome, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (utc_now_iso(), doctor_id, event, target_type,
         str(target_id) if target_id is not None else None, outcome, ip),
    )
    conn.commit()