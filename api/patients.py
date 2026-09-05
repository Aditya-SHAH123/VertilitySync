"""
Patient records, scan-history indexing, and clinical notes.

A "patient" persists across visits: unlike a one-off case, every scan a
doctor uploads for a patient and every note they write accumulates against
the same row, so returning to a patient later shows their full history.

Authorization is NOT implemented in this module (same convention as
cases.py) - every read/write path in api/index.py goes through
auth.doctor_can_access_patient first, so there is exactly one place that
decides who may see what.

Contains no DICOM/pixel handling - patient_studies rows index the
file-based volumes study_store.py actually holds (patient_studies.id is
that study's uuid), they never contain pixel data themselves.
"""

import hashlib
import secrets

from db import get_db, utc_now_iso

VALID_SEX = ("female", "male", "other", "unspecified")


def _default_internal_id():
    return "PT-" + secrets.token_hex(3).upper()


def create_patient(doctor_id, first_name, last_name, date_of_birth=None, sex=None,
                    internal_patient_id=None, medical_record_number=None, path=None):
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    if not first_name or not last_name:
        raise ValueError("First and last name are required.")
    if sex and sex not in VALID_SEX:
        raise ValueError(f"sex must be one of {VALID_SEX}")

    conn = get_db(path)
    now = utc_now_iso()
    internal_id = (internal_patient_id or "").strip() or _default_internal_id()
    try:
        cur = conn.execute(
            "INSERT INTO patients (doctor_id, first_name, last_name, date_of_birth, sex, "
            "internal_patient_id, medical_record_number, created_at, updated_at, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (doctor_id, first_name, last_name, date_of_birth, sex, internal_id,
             (medical_record_number or "").strip() or None, now, now, False),
        )
    except Exception as exc:
        # Unique-constraint violation on (doctor_id, internal_patient_id).
        # "unique" appears in both sqlite3's "UNIQUE constraint failed" and
        # psycopg2's "duplicate key value violates unique constraint".
        # Postgres also requires the failed transaction rolled back before
        # this (cached, per-thread) connection can run another query.
        if conn.dialect == "postgres":
            conn.rollback()
        if "unique" in str(exc).lower():
            raise ValueError(f"You already have a patient with ID {internal_id!r}.") from exc
        raise
    new_id = cur.fetchone()["id"]
    conn.commit()
    return get_patient(new_id, path=path)


def get_patient(patient_id, path=None):
    conn = get_db(path)
    return conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()


def update_patient(patient_id, first_name=None, last_name=None, date_of_birth=None,
                    sex=None, medical_record_number=None, path=None):
    conn = get_db(path)
    fields, params = [], []
    if first_name is not None:
        fields.append("first_name = ?"); params.append(first_name.strip())
    if last_name is not None:
        fields.append("last_name = ?"); params.append(last_name.strip())
    if date_of_birth is not None:
        fields.append("date_of_birth = ?"); params.append(date_of_birth)
    if sex is not None:
        if sex not in VALID_SEX:
            raise ValueError(f"sex must be one of {VALID_SEX}")
        fields.append("sex = ?"); params.append(sex)
    if medical_record_number is not None:
        fields.append("medical_record_number = ?"); params.append(medical_record_number.strip() or None)
    if not fields:
        return get_patient(patient_id, path=path)
    fields.append("updated_at = ?"); params.append(utc_now_iso())
    params.append(patient_id)
    conn.execute(f"UPDATE patients SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    return get_patient(patient_id, path=path)


def archive_patient(patient_id, archived=True, path=None):
    conn = get_db(path)
    conn.execute("UPDATE patients SET archived = ?, updated_at = ? WHERE id = ?",
                 (bool(archived), utc_now_iso(), patient_id))
    conn.commit()


def search_patients(doctor_id, q=None, include_archived=False, path=None):
    """Search by name, internal ID, or MRN. Always scoped to one doctor -
    this is what makes the search authorization-safe."""
    conn = get_db(path)
    sql = "SELECT * FROM patients WHERE doctor_id = ?"
    params = [doctor_id]
    if not include_archived:
        sql += " AND archived = ?"
        params.append(False)
    if q:
        like = f"%{q.strip()}%"
        sql += (" AND (first_name LIKE ? OR last_name LIKE ? OR "
                "(first_name || ' ' || last_name) LIKE ? OR "
                "internal_patient_id LIKE ? OR medical_record_number LIKE ?)")
        params.extend([like, like, like, like, like])
    sql += " ORDER BY updated_at DESC"
    return conn.execute(sql, params).fetchall()


def patient_to_dict(row, study_count=None, last_scan_at=None):
    return {
        "id": row["id"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "full_name": f"{row['first_name']} {row['last_name']}",
        "date_of_birth": row["date_of_birth"],
        "sex": row["sex"],
        "internal_patient_id": row["internal_patient_id"],
        "medical_record_number": row["medical_record_number"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived": bool(row["archived"]),
        "study_count": study_count,
        "last_scan_at": last_scan_at,
    }


# ---------------------------------------------------------------------------
# Scan history / duplicate detection
# ---------------------------------------------------------------------------

def compute_scan_hash(ordered_entries):
    """Deterministic content fingerprint: SHA-256 over the raw PixelData of
    every slice, in physical (already spatially-ordered) order. Two uploads
    with different filenames but identical pixel content hash identically;
    two uploads of the same filenames with different content do not."""
    h = hashlib.sha256()
    for entry in ordered_entries:
        pixel_data = getattr(entry["ds"], "PixelData", None)
        h.update(pixel_data if pixel_data is not None else b"")
    return h.hexdigest()


def find_duplicate_study(patient_id, study_instance_uid, scan_hash, path=None):
    """Duplicate = same patient AND (same StudyInstanceUID when both sides
    have one, OR identical pixel-content hash). Never compares filenames."""
    conn = get_db(path)
    if study_instance_uid:
        row = conn.execute(
            "SELECT * FROM patient_studies WHERE patient_id = ? AND study_instance_uid = ?",
            (patient_id, study_instance_uid),
        ).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM patient_studies WHERE patient_id = ? AND scan_hash = ?",
        (patient_id, scan_hash),
    ).fetchone()


def add_study_record(study_id, patient_id, doctor_id, scan_hash, study_instance_uid=None,
                      series_instance_uid=None, modality=None, scan_date=None,
                      slice_count=None, processing_status="ready", path=None):
    conn = get_db(path)
    conn.execute(
        "INSERT INTO patient_studies (id, patient_id, doctor_id, study_instance_uid, "
        "series_instance_uid, modality, scan_date, slice_count, scan_hash, "
        "processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (study_id, patient_id, doctor_id, study_instance_uid, series_instance_uid,
         modality, scan_date, slice_count, scan_hash, processing_status, utc_now_iso()),
    )
    conn.commit()


def get_study_record(study_id, path=None):
    conn = get_db(path)
    return conn.execute("SELECT * FROM patient_studies WHERE id = ?", (study_id,)).fetchone()


def list_studies_for_patient(patient_id, path=None):
    conn = get_db(path)
    return conn.execute(
        "SELECT * FROM patient_studies WHERE patient_id = ? ORDER BY created_at DESC",
        (patient_id,),
    ).fetchall()


def study_to_dict(row):
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "study_instance_uid": row["study_instance_uid"],
        "series_instance_uid": row["series_instance_uid"],
        "modality": row["modality"],
        "scan_date": row["scan_date"],
        "slice_count": row["slice_count"],
        "processing_status": row["processing_status"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Clinical notes (patient-level when study_id is None, scan-level otherwise)
# ---------------------------------------------------------------------------

def create_note(doctor_id, patient_id, content, title=None, study_id=None,
                 original_content=None, ai_rewritten=False, path=None):
    """original_content defaults to `content` (the common case: a plain
    note). Pass a distinct original_content when the doctor polished a
    brand-new note with AI and accepted the rewrite before ever saving -
    original_content must still be what they actually typed, not the
    AI's wording, even though this is the note's very first save."""
    content = (content or "").strip()
    if not content:
        raise ValueError("Note content cannot be empty.")
    original = (original_content or content).strip()
    conn = get_db(path)
    now = utc_now_iso()
    cur = conn.execute(
        "INSERT INTO clinical_notes (doctor_id, patient_id, study_id, title, "
        "original_content, current_content, ai_rewritten, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (doctor_id, patient_id, study_id, (title or "").strip() or None,
         original, content, bool(ai_rewritten), now, now),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return get_note(new_id, path=path)


def get_note(note_id, path=None):
    conn = get_db(path)
    return conn.execute("SELECT * FROM clinical_notes WHERE id = ?", (note_id,)).fetchone()


def update_note_content(note_id, current_content, ai_rewritten=False, path=None):
    """Updates ONLY current_content (and the ai_rewritten flag).
    original_content is immutable after creation - the whole point of
    keeping it is an honest, permanent record of what the doctor first
    wrote, regardless of any later AI-assisted or manual edit."""
    current_content = (current_content or "").strip()
    if not current_content:
        raise ValueError("Note content cannot be empty.")
    conn = get_db(path)
    conn.execute(
        "UPDATE clinical_notes SET current_content = ?, ai_rewritten = ?, updated_at = ? WHERE id = ?",
        (current_content, bool(ai_rewritten), utc_now_iso(), note_id),
    )
    conn.commit()
    return get_note(note_id, path=path)


def list_notes(patient_id, study_id=None, path=None):
    conn = get_db(path)
    if study_id is not None:
        sql = "SELECT * FROM clinical_notes WHERE patient_id = ? AND study_id = ? ORDER BY created_at DESC"
        params = (patient_id, study_id)
    else:
        sql = "SELECT * FROM clinical_notes WHERE patient_id = ? ORDER BY created_at DESC"
        params = (patient_id,)
    return conn.execute(sql, params).fetchall()


def note_to_dict(row):
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "study_id": row["study_id"],
        "title": row["title"],
        "original_content": row["original_content"],
        "current_content": row["current_content"],
        "ai_rewritten": bool(row["ai_rewritten"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Vital signs
# ---------------------------------------------------------------------------
# Every reading is a new row, never an update-in-place - a patient's trend
# over time is the full history, not a single "current" value. Every
# measurement is optional (a doctor logging just a heart rate shouldn't be
# forced to fill in fields they didn't take), but at least one must be
# present, and each is checked against a generous physiological range -
# not a clinical judgment, just a guard against an obvious typo like a
# heart rate of 3000.

VITAL_RANGES = {
    "heart_rate_bpm": (0, 300, "bpm"),
    "systolic_bp_mmhg": (0, 300, "mmHg"),
    "diastolic_bp_mmhg": (0, 250, "mmHg"),
    "respiratory_rate_bpm": (0, 100, "breaths/min"),
    "temperature_c": (25.0, 45.0, "°C"),
    "oxygen_saturation_pct": (0.0, 100.0, "%"),
    "weight_kg": (0.0, 500.0, "kg"),
    "height_cm": (0.0, 300.0, "cm"),
}


def _validate_vitals(values):
    if all(v is None for v in values.values()):
        raise ValueError("At least one vital-sign value is required.")
    for field, value in values.items():
        if value is None:
            continue
        lo, hi, unit = VITAL_RANGES[field]
        if not (lo <= value <= hi):
            raise ValueError(f"{field.replace('_', ' ')} of {value}{unit} is outside the "
                              f"plausible range ({lo}-{hi}{unit}); check for a data-entry error.")


def create_vital_signs(patient_id, doctor_id, recorded_at=None, heart_rate_bpm=None,
                        systolic_bp_mmhg=None, diastolic_bp_mmhg=None,
                        respiratory_rate_bpm=None, temperature_c=None,
                        oxygen_saturation_pct=None, weight_kg=None, height_cm=None,
                        notes=None, path=None):
    values = {
        "heart_rate_bpm": heart_rate_bpm, "systolic_bp_mmhg": systolic_bp_mmhg,
        "diastolic_bp_mmhg": diastolic_bp_mmhg, "respiratory_rate_bpm": respiratory_rate_bpm,
        "temperature_c": temperature_c, "oxygen_saturation_pct": oxygen_saturation_pct,
        "weight_kg": weight_kg, "height_cm": height_cm,
    }
    _validate_vitals(values)
    conn = get_db(path)
    now = utc_now_iso()
    cur = conn.execute(
        "INSERT INTO vital_signs (patient_id, doctor_id, recorded_at, heart_rate_bpm, "
        "systolic_bp_mmhg, diastolic_bp_mmhg, respiratory_rate_bpm, temperature_c, "
        "oxygen_saturation_pct, weight_kg, height_cm, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (patient_id, doctor_id, recorded_at or now, values["heart_rate_bpm"],
         values["systolic_bp_mmhg"], values["diastolic_bp_mmhg"], values["respiratory_rate_bpm"],
         values["temperature_c"], values["oxygen_saturation_pct"], values["weight_kg"],
         values["height_cm"], (notes or "").strip() or None, now),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return get_vital_signs(new_id, path=path)


def get_vital_signs(vitals_id, path=None):
    conn = get_db(path)
    return conn.execute("SELECT * FROM vital_signs WHERE id = ?", (vitals_id,)).fetchone()


def list_vital_signs(patient_id, path=None):
    conn = get_db(path)
    return conn.execute(
        "SELECT * FROM vital_signs WHERE patient_id = ? ORDER BY recorded_at DESC",
        (patient_id,),
    ).fetchall()


def delete_vital_signs(vitals_id, doctor_id, path=None):
    conn = get_db(path)
    row = conn.execute("SELECT doctor_id FROM vital_signs WHERE id = ?", (vitals_id,)).fetchone()
    if row is None:
        raise KeyError(vitals_id)
    if row["doctor_id"] != doctor_id:
        raise PermissionError("Only the recording doctor may delete this reading.")
    conn.execute("DELETE FROM vital_signs WHERE id = ?", (vitals_id,))
    conn.commit()


def vital_signs_to_dict(row):
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "recorded_at": row["recorded_at"],
        "heart_rate_bpm": row["heart_rate_bpm"],
        "systolic_bp_mmhg": row["systolic_bp_mmhg"],
        "diastolic_bp_mmhg": row["diastolic_bp_mmhg"],
        "respiratory_rate_bpm": row["respiratory_rate_bpm"],
        "temperature_c": row["temperature_c"],
        "oxygen_saturation_pct": row["oxygen_saturation_pct"],
        "weight_kg": row["weight_kg"],
        "height_cm": row["height_cm"],
        "notes": row["notes"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Diagnoses (problem list)
# ---------------------------------------------------------------------------
# Every field is doctor-authored. diagnosis_name is always the doctor's own
# wording; icd10_code/icd10_description (when present) come only from the
# static lookup in api/icd10_reference.py - never inferred, never
# AI-suggested. Status changes are appended to diagnosis_status_history
# rather than overwriting anything, so the full history of a problem
# (e.g. active -> resolved -> active again) is always reconstructable.

DIAGNOSIS_STATUSES = ("active", "resolved", "chronic", "ruled_out")
DIAGNOSIS_SEVERITIES = ("mild", "moderate", "severe", "unspecified")


def create_diagnosis(patient_id, doctor_id, diagnosis_name, icd10_code=None,
                      icd10_description=None, status="active", severity=None,
                      onset_date=None, diagnosed_date=None, study_id=None,
                      notes=None, path=None):
    diagnosis_name = (diagnosis_name or "").strip()
    if not diagnosis_name:
        raise ValueError("A diagnosis name is required.")
    if status not in DIAGNOSIS_STATUSES:
        raise ValueError(f"status must be one of {DIAGNOSIS_STATUSES}")
    if severity and severity not in DIAGNOSIS_SEVERITIES:
        raise ValueError(f"severity must be one of {DIAGNOSIS_SEVERITIES}")

    conn = get_db(path)
    now = utc_now_iso()
    cur = conn.execute(
        "INSERT INTO diagnoses (patient_id, doctor_id, diagnosis_name, icd10_code, "
        "icd10_description, status, severity, onset_date, diagnosed_date, study_id, "
        "notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (patient_id, doctor_id, diagnosis_name, icd10_code or None, icd10_description or None,
         status, severity or None, onset_date or None, diagnosed_date or now[:10],
         study_id or None, (notes or "").strip() or None, now, now),
    )
    new_id = cur.fetchone()["id"]
    # The history trail starts here, same shape as every later transition.
    conn.execute(
        "INSERT INTO diagnosis_status_history (diagnosis_id, doctor_id, old_status, "
        "new_status, note, changed_at) VALUES (?, ?, NULL, ?, ?, ?)",
        (new_id, doctor_id, status, "Diagnosis recorded.", now),
    )
    conn.commit()
    return get_diagnosis(new_id, path=path)


def get_diagnosis(diagnosis_id, path=None):
    conn = get_db(path)
    return conn.execute("SELECT * FROM diagnoses WHERE id = ?", (diagnosis_id,)).fetchone()


def list_diagnoses(patient_id, status=None, path=None):
    conn = get_db(path)
    sql = "SELECT * FROM diagnoses WHERE patient_id = ?"
    params = [patient_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY diagnosed_date DESC, id DESC"
    return conn.execute(sql, params).fetchall()


def update_diagnosis(diagnosis_id, diagnosis_name=None, icd10_code=None, icd10_description=None,
                      severity=None, onset_date=None, notes=None, study_id=None, path=None):
    """Edits the doctor-authored fields. Does NOT change status - use
    update_diagnosis_status() for that, so every status change is always
    captured in the history table with no code path that can skip it."""
    conn = get_db(path)
    fields, params = [], []
    if diagnosis_name is not None:
        diagnosis_name = diagnosis_name.strip()
        if not diagnosis_name:
            raise ValueError("Diagnosis name cannot be empty.")
        fields.append("diagnosis_name = ?"); params.append(diagnosis_name)
    if icd10_code is not None:
        fields.append("icd10_code = ?"); params.append(icd10_code or None)
    if icd10_description is not None:
        fields.append("icd10_description = ?"); params.append(icd10_description or None)
    if severity is not None:
        if severity and severity not in DIAGNOSIS_SEVERITIES:
            raise ValueError(f"severity must be one of {DIAGNOSIS_SEVERITIES}")
        fields.append("severity = ?"); params.append(severity or None)
    if onset_date is not None:
        fields.append("onset_date = ?"); params.append(onset_date or None)
    if notes is not None:
        fields.append("notes = ?"); params.append(notes.strip() or None)
    if study_id is not None:
        fields.append("study_id = ?"); params.append(study_id or None)
    if not fields:
        return get_diagnosis(diagnosis_id, path=path)
    fields.append("updated_at = ?"); params.append(utc_now_iso())
    params.append(diagnosis_id)
    conn.execute(f"UPDATE diagnoses SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    return get_diagnosis(diagnosis_id, path=path)


def update_diagnosis_status(diagnosis_id, doctor_id, new_status, note=None, path=None):
    if new_status not in DIAGNOSIS_STATUSES:
        raise ValueError(f"status must be one of {DIAGNOSIS_STATUSES}")
    current = get_diagnosis(diagnosis_id, path=path)
    if current is None:
        raise KeyError(diagnosis_id)
    conn = get_db(path)
    now = utc_now_iso()
    conn.execute("UPDATE diagnoses SET status = ?, updated_at = ? WHERE id = ?",
                 (new_status, now, diagnosis_id))
    conn.execute(
        "INSERT INTO diagnosis_status_history (diagnosis_id, doctor_id, old_status, "
        "new_status, note, changed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (diagnosis_id, doctor_id, current["status"], new_status, (note or "").strip() or None, now),
    )
    conn.commit()
    return get_diagnosis(diagnosis_id, path=path)


def list_diagnosis_history(diagnosis_id, path=None):
    conn = get_db(path)
    return conn.execute(
        "SELECT * FROM diagnosis_status_history WHERE diagnosis_id = ? ORDER BY changed_at DESC, id DESC",
        (diagnosis_id,),
    ).fetchall()


def diagnosis_to_dict(row):
    return {
        "id": row["id"],
        "patient_id": row["patient_id"],
        "diagnosis_name": row["diagnosis_name"],
        "icd10_code": row["icd10_code"],
        "icd10_description": row["icd10_description"],
        "status": row["status"],
        "severity": row["severity"],
        "onset_date": row["onset_date"],
        "diagnosed_date": row["diagnosed_date"],
        "study_id": row["study_id"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def diagnosis_history_to_dict(row):
    return {
        "id": row["id"],
        "diagnosis_id": row["diagnosis_id"],
        "old_status": row["old_status"],
        "new_status": row["new_status"],
        "note": row["note"],
        "changed_at": row["changed_at"],
    }
