"""
Persistence for doctor accounts, cases, patients, notes, and the audit log.

Two backends, selected by environment variable, with an identical calling
convention (`conn.execute(sql_with_question_mark_placeholders, params)`)
so every other module (auth.py, cases.py, patients.py) is written once and
runs against either:

  * SQLite (default) - stdlib `sqlite3`, a single local file. Good for
    local development; NOT durable on a serverless host (see below).
  * Postgres, when DATABASE_URL is set (e.g. a Supabase connection
    string) - via psycopg2. This is the real, durable, shared-across-
    instances option, and what a live Vercel deployment should use.

DEPLOYMENT LIMITATION (documented, not worked around, for the SQLite path):
    On Vercel's serverless Python runtime the filesystem is ephemeral and
    not shared between function instances, so a SQLite file under /tmp
    lives only as long as one instance. Accounts/cases/patients created on
    one invocation may not be visible on another. Setting DATABASE_URL to a
    Postgres connection string (see SUPABASE_SCHEMA.sql) is what actually
    fixes this - the schema and every query here are plain, portable SQL
    specifically so that swap needs no other code changes.

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

# Postgres connection string (e.g. Supabase). When set, this backend is used
# instead of SQLite - see SUPABASE_SCHEMA.sql for the matching schema and
# DATABASE_DESIGN.md for the migration story.
DATABASE_URL = os.environ.get("DATABASE_URL")

# Default to a local file next to the repo for development; override with
# DATABASE_PATH (e.g. /tmp/vitalitysync.db on a serverless host). Ignored
# entirely when DATABASE_URL is set.
DEFAULT_DB_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vitalitysync.db"),
)

_local = threading.local()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Portable connection: lets every other module write one set of queries
# ("?" placeholders, dict-like rows, cur.lastrowid via a RETURNING pattern)
# that runs unchanged against SQLite or Postgres.
# ---------------------------------------------------------------------------

class _PortableCursor:
    def __init__(self, raw_cursor, dialect):
        self._cur = raw_cursor
        self._dialect = dialect

    def execute(self, sql, params=()):
        if self._dialect == "postgres":
            sql = sql.replace("?", "%s")
        self._cur.execute(sql, params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        # Only meaningful for SQLite; Postgres inserts here use
        # "RETURNING id" + fetchone()["id"] instead - see auth.py/cases.py/
        # patients.py's create_* functions.
        return getattr(self._cur, "lastrowid", None)


class _PortableConnection:
    """Wraps either a sqlite3.Connection or a psycopg2 connection behind the
    same execute()/executescript()/commit()/close() surface that db.py's
    callers already use."""

    def __init__(self, raw_conn, dialect):
        self._conn = raw_conn
        self.dialect = dialect  # "sqlite" or "postgres"

    def execute(self, sql, params=()):
        if self.dialect == "postgres":
            from psycopg2.extras import RealDictCursor
            raw_cursor = self._conn.cursor(cursor_factory=RealDictCursor)
        else:
            raw_cursor = self._conn.cursor()
        return _PortableCursor(raw_cursor, self.dialect).execute(sql, params)

    def executescript(self, sql):
        """DDL only, no parameters - used solely for the CREATE TABLE
        schema. psycopg2's cursor.execute() with no bind parameters uses
        PQexec(), which (unlike PQexecParams()) accepts a semicolon-
        separated multi-statement string, so this works unmodified on both
        backends."""
        if self.dialect == "sqlite":
            self._conn.executescript(sql)
        else:
            cur = self._conn.cursor()
            cur.execute(sql)
        self.commit()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _connect_sqlite(db_path):
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
    wrapped = _PortableConnection(conn, "sqlite")
    wrapped.execute("PRAGMA foreign_keys = ON")
    return wrapped


def _connect_postgres(database_url):
    import psycopg2
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    return _PortableConnection(conn, "postgres")


def get_db(path=None):
    """Per-thread connection. Flask's dev server and test client are
    threaded, and neither sqlite3 nor psycopg2 connections are safe to
    share across threads.

    `path` overrides DATABASE_PATH for the SQLite backend only (used by
    tests for isolation); it has no effect once DATABASE_URL selects
    Postgres, since tests never point that env var at a real database.
    """
    target = path or DATABASE_URL or DEFAULT_DB_PATH
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "target", None) == target:
        return conn
    if conn is not None:
        conn.close()

    if DATABASE_URL and not path:
        conn = _connect_postgres(DATABASE_URL)
    else:
        conn = _connect_sqlite(target)

    _local.conn = conn
    _local.target = target
    return conn


def close_db():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.target = None


# SQLite dialect: AUTOINCREMENT, TEXT timestamps (matches how the rest of
# this codebase already reads/writes them, e.g. utc_now_iso()).
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    supabase_user_id   TEXT
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

-- Vital signs: a point-in-time reading, not a running "current state" -
-- every entry is retained (see api/patients.py's create_vital_signs), so a
-- patient's trend over time is just "list every row, ordered by
-- recorded_at". Every measurement column is nullable - a single reading
-- rarely includes all of them, and a doctor should be able to log just a
-- heart rate without being forced to fill in fields they didn't measure.
CREATE TABLE IF NOT EXISTS vital_signs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id             INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    doctor_id              INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    recorded_at            TEXT NOT NULL,
    heart_rate_bpm         INTEGER,
    systolic_bp_mmhg       INTEGER,
    diastolic_bp_mmhg      INTEGER,
    respiratory_rate_bpm   INTEGER,
    temperature_c          REAL,
    oxygen_saturation_pct  REAL,
    weight_kg              REAL,
    height_cm              REAL,
    notes                  TEXT,
    created_at             TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_vital_signs_patient ON vital_signs(patient_id);
"""

# Postgres dialect: SERIAL, TIMESTAMPTZ, BOOLEAN. Kept in sync by hand with
# SUPABASE_SCHEMA.sql (the copy meant to be pasted into Supabase's SQL
# Editor directly) - this copy exists so init_db()/reset_db() are also
# correct if DATABASE_URL points at a fresh Postgres database that hasn't
# had that script run against it yet.
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors (
    id                 SERIAL PRIMARY KEY,
    email              TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    supabase_user_id   TEXT
);

CREATE TABLE IF NOT EXISTS cases (
    id                SERIAL PRIMARY KEY,
    case_ref          TEXT NOT NULL UNIQUE,
    owner_doctor_id   INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'needs_review',
    is_demo           BOOLEAN NOT NULL DEFAULT false,
    study_id          TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS case_access (
    case_id    INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    doctor_id  INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'viewer',
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, doctor_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id                SERIAL PRIMARY KEY,
    case_id           INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    author_doctor_id  INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           SERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    doctor_id    INTEGER,
    event        TEXT NOT NULL,
    target_type  TEXT,
    target_id    TEXT,
    outcome      TEXT NOT NULL DEFAULT 'success',
    ip           TEXT
);

CREATE TABLE IF NOT EXISTS patients (
    id                     SERIAL PRIMARY KEY,
    doctor_id              INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    first_name             TEXT NOT NULL,
    last_name              TEXT NOT NULL,
    date_of_birth          TEXT,
    sex                    TEXT,
    internal_patient_id    TEXT NOT NULL,
    medical_record_number  TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived               BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (doctor_id, internal_patient_id)
);

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
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clinical_notes (
    id                SERIAL PRIMARY KEY,
    doctor_id         INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    patient_id        INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    study_id          TEXT REFERENCES patient_studies(id) ON DELETE CASCADE,
    title             TEXT,
    original_content  TEXT NOT NULL,
    current_content   TEXT NOT NULL,
    ai_rewritten      BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vital_signs (
    id                     SERIAL PRIMARY KEY,
    patient_id             INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    doctor_id              INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    recorded_at            TIMESTAMPTZ NOT NULL,
    heart_rate_bpm         INTEGER,
    systolic_bp_mmhg       INTEGER,
    diastolic_bp_mmhg      INTEGER,
    respiratory_rate_bpm   INTEGER,
    temperature_c          REAL,
    oxygen_saturation_pct  REAL,
    weight_kg              REAL,
    height_cm              REAL,
    notes                  TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
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
CREATE INDEX IF NOT EXISTS idx_vital_signs_patient ON vital_signs(patient_id);
"""


def _schema_for(conn):
    return SQLITE_SCHEMA if conn.dialect == "sqlite" else POSTGRES_SCHEMA


def _ensure_column(conn, table, column, coltype):
    """Adds a column to an already-existing table if it's missing.

    `CREATE TABLE IF NOT EXISTS` (used everywhere else in this file) does
    nothing for a table that already exists with an older shape - a real
    database created before `doctors.supabase_user_id` existed needs this
    to pick the column up without a destructive drop/recreate. Postgres
    supports `ADD COLUMN IF NOT EXISTS` directly; SQLite doesn't, so the
    SQLite path just attempts the ALTER and swallows the "duplicate
    column" error if it's already there.
    """
    if conn.dialect == "postgres":
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
        conn.commit()
    else:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def init_db(path=None):
    conn = get_db(path)
    conn.executescript(_schema_for(conn))
    _ensure_column(conn, "doctors", "supabase_user_id", "TEXT")
    return conn


def reset_db(path=None):
    """Drops and recreates every table. Test-support only. Never call this
    against a real Postgres/Supabase database - it is only exercised by the
    test suite, which always uses SQLite (tests never set DATABASE_URL)."""
    conn = get_db(path)
    for table in ("audit_log", "vital_signs", "clinical_notes", "patient_studies", "patients",
                  "notes", "case_access", "cases", "doctors"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    conn.executescript(_schema_for(conn))
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
