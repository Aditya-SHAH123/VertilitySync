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
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (doctor_id, first_name, last_name, date_of_birth, sex, internal_id,
             (medical_record_number or "").strip() or None, now, now),
        )
    except Exception as exc:  # sqlite3.IntegrityError - unique (doctor_id, internal_patient_id)
        if "UNIQUE" in str(exc):
            raise ValueError(f"You already have a patient with ID {internal_id!r}.") from exc
        raise
    conn.commit()
    return get_patient(cur.lastrowid, path=path)


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
                 (1 if archived else 0, utc_now_iso(), patient_id))
    conn.commit()


def search_patients(doctor_id, q=None, include_archived=False, path=None):
    """Search by name, internal ID, or MRN. Always scoped to one doctor -
    this is what makes the search authorization-safe."""
    conn = get_db(path)
    sql = "SELECT * FROM patients WHERE doctor_id = ?"
    params = [doctor_id]
    if not include_archived:
        sql += " AND archived = 0"
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
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doctor_id, patient_id, study_id, (title or "").strip() or None,
         original, content, 1 if ai_rewritten else 0, now, now),
    )
    conn.commit()
    return get_note(cur.lastrowid, path=path)


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
        (current_content, 1 if ai_rewritten else 0, utc_now_iso(), note_id),
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
