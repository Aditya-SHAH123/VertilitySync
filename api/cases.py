"""
Case records and notes.

A "case" is an investigation container owned by a doctor. It holds a title,
a status, optional notes, and optionally a link to an imported imaging study
(by opaque study id only - no pixel data or patient metadata is stored here).

Authorization is NOT implemented in this module. Every read/write path goes
through `auth.authorize_case_or_none` / `auth.doctor_can_access_case` in the
route layer, so there is exactly one place that decides who may see what.
"""

import secrets

from db import get_db, utc_now_iso

VALID_STATUSES = ("needs_review", "processing", "ready", "archived")

STATUS_LABELS = {
    "needs_review": "Needs Review",
    "processing": "Processing",
    "ready": "Ready",
    "archived": "Archived",
}


def generate_case_ref():
    """Opaque, unguessable public identifier. Deliberately not sequential:
    a sequential id would let a signed-in doctor enumerate the existence of
    other doctors' cases even though access is denied."""
    return "CASE-" + secrets.token_hex(6).upper()


def create_case(owner_doctor_id, title, status="needs_review", is_demo=False,
                 study_id=None, path=None):
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    conn = get_db(path)
    now = utc_now_iso()
    case_ref = generate_case_ref()
    cur = conn.execute(
        "INSERT INTO cases (case_ref, owner_doctor_id, title, status, is_demo, study_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (case_ref, owner_doctor_id, title, status, 1 if is_demo else 0, study_id, now, now),
    )
    case_id = cur.lastrowid
    # The owner's access is an explicit grant row, so authorization has a
    # single uniform code path (see auth.doctor_can_access_case).
    conn.execute(
        "INSERT INTO case_access (case_id, doctor_id, role, granted_at) VALUES (?, ?, 'owner', ?)",
        (case_id, owner_doctor_id, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()


def grant_access(case_id, doctor_id, role="viewer", path=None):
    conn = get_db(path)
    conn.execute(
        "INSERT OR REPLACE INTO case_access (case_id, doctor_id, role, granted_at) VALUES (?, ?, ?, ?)",
        (case_id, doctor_id, role, utc_now_iso()),
    )
    conn.commit()


def list_cases_for_doctor(doctor_id, status=None, query=None, path=None):
    """Only returns cases the doctor has an explicit grant for. The join
    against case_access is what enforces this at the query level, so a
    filtering bug cannot leak another doctor's case into the list."""
    conn = get_db(path)
    sql = (
        "SELECT c.* FROM cases c "
        "JOIN case_access a ON a.case_id = c.id "
        "WHERE a.doctor_id = ?"
    )
    params = [doctor_id]
    if status and status in VALID_STATUSES:
        sql += " AND c.status = ?"
        params.append(status)
    if query:
        sql += " AND (c.title LIKE ? OR c.case_ref LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like])
    sql += " ORDER BY c.updated_at DESC"
    return conn.execute(sql, params).fetchall()


def update_case_status(case_id, status, path=None):
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    conn = get_db(path)
    conn.execute("UPDATE cases SET status = ?, updated_at = ? WHERE id = ?",
                 (status, utc_now_iso(), case_id))
    conn.commit()


def attach_study(case_id, study_id, path=None):
    conn = get_db(path)
    conn.execute("UPDATE cases SET study_id = ?, updated_at = ? WHERE id = ?",
                 (study_id, utc_now_iso(), case_id))
    conn.commit()


def case_to_dict(row):
    return {
        "case_ref": row["case_ref"],
        "title": row["title"],
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "is_demo": bool(row["is_demo"]),
        "study_id": row["study_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def add_note(case_id, author_doctor_id, content, path=None):
    content = (content or "").strip()
    if not content:
        raise ValueError("Note content cannot be empty.")
    conn = get_db(path)
    now = utc_now_iso()
    cur = conn.execute(
        "INSERT INTO notes (case_id, author_doctor_id, content, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (case_id, author_doctor_id, content, now, now),
    )
    conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
    conn.commit()
    return cur.lastrowid


def update_note(note_id, content, path=None):
    content = (content or "").strip()
    if not content:
        raise ValueError("Note content cannot be empty.")
    conn = get_db(path)
    conn.execute("UPDATE notes SET content = ?, updated_at = ? WHERE id = ?",
                 (content, utc_now_iso(), note_id))
    conn.commit()


def get_note(note_id, path=None):
    conn = get_db(path)
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()


def list_notes(case_id, path=None):
    conn = get_db(path)
    return conn.execute(
        "SELECT n.*, d.display_name AS author_name FROM notes n "
        "JOIN doctors d ON d.id = n.author_doctor_id "
        "WHERE n.case_id = ? ORDER BY n.created_at DESC",
        (case_id,),
    ).fetchall()


def note_to_dict(row):
    return {
        "id": row["id"],
        "content": row["content"],
        "author_name": row["author_name"] if "author_name" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
