"""
Supabase Auth integration for doctor sign-up/sign-in.

NAMING: this file is deliberately NOT called supabase_auth.py. The
official `supabase` package depends on its own module literally named
`supabase_auth` (the GoTrue client, imported as `from supabase_auth.errors
import ...`). Since api/ sits on sys.path, a same-named local file shadows
that real package and breaks every import inside the `supabase` library
with `ModuleNotFoundError: No module named 'supabase_auth.errors';
'supabase_auth' is not a package` - this was hit and fixed during
development, not a hypothetical concern.

WHY THIS IS SEPARATE FROM auth.py
    auth.py's create_doctor()/verify_credentials() manage a fully local
    `doctors` table with Werkzeug password hashes. This module instead
    delegates credential storage and verification to Supabase's hosted
    Auth service (GoTrue), reached over HTTPS via SUPABASE_URL +
    SUPABASE_KEY - a REST call, not a database connection, so it works
    even when DATABASE_URL (a direct Postgres connection) can't resolve
    (see DATABASE_DESIGN.md's note on the direct-connection host being
    IPv6-only on some networks).

    A local `doctors` row is still created for every Supabase-
    authenticated account (see auth.create_doctor_from_supabase) so every
    existing authorization check - case/patient ownership, the audit log -
    keeps working unchanged, keyed by our own integer doctor id. This
    module only verifies identity; it authorizes nothing by itself.

FALLBACK
    If SUPABASE_URL/SUPABASE_KEY aren't set, every function here returns
    status 'NOT_CONFIGURED' rather than raising - the routes in index.py
    fall back to the local auth.create_doctor()/verify_credentials() path
    in that case, so local development and the test suite (which never
    set these vars) are unaffected.

CONFIGURATION
    SUPABASE_URL - the project URL, e.g. https://xxxx.supabase.co
    SUPABASE_KEY - the anon public API key. Never the service_role key
    here - this should only ever be able to do what a signed-out browser
    could do (sign up / sign in), not bypass Supabase's own auth rules.
"""

import os


def _client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None
    from supabase import create_client
    return create_client(url, key)


def is_configured():
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"))


def sign_up(email, password):
    """Returns:
        {'status': 'OK', 'user_id': str, 'confirmed': bool}
        {'status': 'FAIL', 'message': str}          - Supabase rejected the request
        {'status': 'NOT_CONFIGURED'}
    `confirmed` is False when the project requires email confirmation
    before the account can sign in - the caller must not treat that as a
    logged-in session.
    """
    client = _client()
    if client is None:
        return {"status": "NOT_CONFIGURED"}
    try:
        resp = client.auth.sign_up({"email": email, "password": password})
    except Exception as exc:  # noqa: BLE001 - Supabase's own error text is already user-facing
        return {"status": "FAIL", "message": str(exc) or "Could not create the account."}
    if resp.user is None:
        return {"status": "FAIL", "message": "Could not create the account."}
    return {"status": "OK", "user_id": resp.user.id, "confirmed": resp.session is not None}


def sign_in(email, password):
    """Returns {'status': 'OK', 'user_id': str}, {'status': 'FAIL', 'message': str},
    or {'status': 'NOT_CONFIGURED'}. The failure message is always the same
    generic text - Supabase's own error detail is not passed through here,
    matching this app's existing anti-enumeration convention for login."""
    client = _client()
    if client is None:
        return {"status": "NOT_CONFIGURED"}
    try:
        resp = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception:  # noqa: BLE001
        return {"status": "FAIL", "message": "Invalid email or password."}
    if resp.user is None:
        return {"status": "FAIL", "message": "Invalid email or password."}
    return {"status": "OK", "user_id": resp.user.id}
