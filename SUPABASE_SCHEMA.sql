-- VitalitySync schema for Supabase (Postgres). Paste into Supabase's
-- SQL Editor and run once. Mirrors api/db.py's SQLite schema, translated
-- to Postgres dialect (SERIAL instead of AUTOINCREMENT, TIMESTAMPTZ
-- instead of TEXT for timestamps, ON CONFLICT instead of INSERT OR REPLACE).

CREATE TABLE IF NOT EXISTS doctors (
    id             SERIAL PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    display_name   TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
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
