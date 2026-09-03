"""
Doctor authentication and case authorization.

Security properties this module is responsible for:
  * Passwords are stored only as Werkzeug PBKDF2/scrypt hashes - never
    plaintext, never reversible, never hardcoded.
  * The signed-in identity lives in Flask's signed session cookie (HttpOnly,
    SameSite=Lax, Secure when served over HTTPS). The cookie carries only a
    doctor id; it is not trusted for authorization by itself - every
    protected route re-reads the doctor row and re-checks case access
    server-side on each request.
  * Authorization is enforced in the backend for every protected route and
    API endpoint. Hiding a link in a template is never treated as protection.
  * Case access is a single explicit query against `case_access`. Guessing or
    editing a case id in the URL cannot grant access, because the check is
    "is there a grant row for (this case, this doctor)" rather than anything
    derived from the request.
"""

import functools
import os

from flask import session, request, jsonify, redirect, url_for, g
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db, record_audit, utc_now_iso

SESSION_KEY = "doctor_id"


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------

def create_doctor(email, password, display_name, path=None):
    """Creates a doctor with a hashed password. Raises ValueError on a
    duplicate email or a password that fails the minimum policy."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("A valid email address is required.")
    if not password or len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    if not display_name or not display_name.strip():
        raise ValueError("A display name is required.")

    conn = get_db(path)
    existing = conn.execute("SELECT id FROM doctors WHERE email = ?", (email,)).fetchone()
    if existing:
        raise ValueError("An account with that email already exists.")

    cur = conn.execute(
        "INSERT INTO doctors (email, password_hash, display_name, created_at) "
        "VALUES (?, ?, ?, ?) RETURNING id",
        (email, generate_password_hash(password), display_name.strip(), utc_now_iso()),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return new_id


def verify_credentials(email, password, path=None):
    """Returns the doctor row on success, or None. Runs the hash comparison
    even when the account is missing so a failed lookup and a wrong password
    take similar time and do not reveal which accounts exist."""
    email = (email or "").strip().lower()
    conn = get_db(path)
    row = conn.execute("SELECT * FROM doctors WHERE email = ?", (email,)).fetchone()
    if row is None:
        # Dummy comparison to even out timing between "no such user" and
        # "wrong password".
        check_password_hash(generate_password_hash("timing-equalizer"), password or "")
        return None
    if not check_password_hash(row["password_hash"], password or ""):
        return None
    return row


def get_doctor(doctor_id, path=None):
    if doctor_id is None:
        return None
    conn = get_db(path)
    return conn.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def login_session(doctor_row):
    session.clear()
    session[SESSION_KEY] = doctor_row["id"]
    session.permanent = True


def logout_session():
    session.clear()


def current_doctor():
    """Re-reads the doctor from the database on every call. The session
    cookie is only a pointer; a deleted account stops working immediately."""
    if "current_doctor" in g:
        return g.current_doctor
    doctor = get_doctor(session.get(SESSION_KEY))
    g.current_doctor = doctor
    return doctor


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"


# ---------------------------------------------------------------------------
# Route protection
# ---------------------------------------------------------------------------

def login_required(view):
    """Protects an HTML page: anonymous users are redirected to the login
    page rather than shown the content."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_doctor() is None:
            return redirect(url_for("login_page", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def api_login_required(view):
    """Protects a JSON endpoint: anonymous users get 401 JSON, never a
    redirect to an HTML page."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_doctor() is None:
            return jsonify({"status": "UNAUTHENTICATED",
                            "message": "Sign in as a doctor to access this resource."}), 401
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Case authorization
# ---------------------------------------------------------------------------

def doctor_can_access_case(doctor_id, case_id, path=None):
    """Single source of truth for case authorization.

    Returns True only when an explicit grant row exists. Case ownership is
    represented by a grant row created alongside the case, so there is no
    second, divergent code path that could drift out of sync.
    """
    if doctor_id is None or case_id is None:
        return False
    conn = get_db(path)
    row = conn.execute(
        "SELECT 1 FROM case_access WHERE case_id = ? AND doctor_id = ?",
        (case_id, doctor_id),
    ).fetchone()
    return row is not None


def doctor_can_access_patient(doctor_id, patient_row, path=None):
    """Patient authorization is ownership-based (unlike the case_access grant
    table): a patient record belongs to the doctor who created it. Simpler
    than cases' sharing model, and sufficient for this workflow - see
    ARCHITECTURE_AUDIT.md if multi-doctor patient sharing is ever needed."""
    if doctor_id is None or patient_row is None:
        return False
    return patient_row["doctor_id"] == doctor_id


def authorize_case_or_none(case_ref, doctor_id, path=None):
    """Resolves a public case reference to a row the given doctor may see.

    Returns (case_row, None) on success, or (None, reason) where reason is
    'not_found' or 'forbidden'. Callers must respond identically for both -
    see the route handlers - so that probing case refs cannot distinguish an
    existing case from a missing one.
    """
    conn = get_db(path)
    row = conn.execute("SELECT * FROM cases WHERE case_ref = ?", (case_ref,)).fetchone()
    if row is None:
        return None, "not_found"
    if not doctor_can_access_case(doctor_id, row["id"], path=path):
        return None, "forbidden"
    return row, None


def configure_session_cookie(app):
    """Applies session hardening. SECRET_KEY must come from the environment
    in any real deployment; a missing key falls back to an ephemeral random
    value so sessions simply do not survive a restart rather than running on
    a predictable hardcoded secret."""
    secret = os.environ.get("SECRET_KEY")
    app.config["SECRET_KEY"] = secret or os.urandom(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Only send the cookie over HTTPS when not running locally.
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") != "development" and bool(secret)
    return secret is not None
