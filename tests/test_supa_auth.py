"""
Tests for the Supabase Auth integration (api/supabase_auth.py) and its
wiring into /api/auth/signup and /api/auth/login in api/index.py.

supabase_auth.sign_up/sign_in/is_configured are monkeypatched here rather
than calling the real Supabase API - consistent with how ai_notes.polish_note
is stubbed in test_patients.py: tests must be deterministic, offline, and
never depend on a real external service being reachable or in any
particular state.

Same script style as test_app.py (check()/PASS/FAIL/main()) so run_all.py
picks it up uniformly.
"""

import os
import sys

os.environ.setdefault('DATABASE_PATH', '/tmp/vitalitysync_test_supabase_auth.db')
if os.path.exists(os.environ['DATABASE_PATH']):
    os.remove(os.environ['DATABASE_PATH'])
# The whole point of this suite is to control exactly when supabase_auth
# "looks configured" via monkeypatching is_configured() - the real env vars
# must never leak in underneath that.
os.environ['DATABASE_URL'] = ''
os.environ['SUPABASE_URL'] = ''
os.environ['SUPABASE_KEY'] = ''
os.environ.setdefault('STUDY_STORE_PATH', '/tmp/vitalitysync_test_supabase_auth_studies')
os.environ.setdefault('IMAGING_DATABASE_URL', 'sqlite:////tmp/vitalitysync_test_supabase_auth_imaging.db')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from index import app  # noqa: E402
import db as dbmod  # noqa: E402
import auth as authmod  # noqa: E402
import supa_auth as supabase_auth  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'PASS: {name}')
    else:
        FAIL.append(name)
        print(f'FAIL: {name} {extra}')


class FakeSupabase:
    """In-memory stand-in for the real Supabase Auth service, keyed by
    email, so sign_up/sign_in behave consistently across a test."""

    def __init__(self):
        self.users = {}  # email -> {'user_id': str, 'password': str, 'confirmed': bool}
        self._next_id = 1

    def sign_up(self, email, password, confirmed=True):
        if email in self.users:
            return {'status': 'FAIL', 'message': 'User already registered'}
        user_id = f'supabase-user-{self._next_id}'
        self._next_id += 1
        self.users[email] = {'user_id': user_id, 'password': password, 'confirmed': confirmed}
        return {'status': 'OK', 'user_id': user_id, 'confirmed': confirmed}

    def sign_in(self, email, password):
        record = self.users.get(email)
        if record is None or record['password'] != password:
            return {'status': 'FAIL', 'message': 'Invalid email or password.'}
        return {'status': 'OK', 'user_id': record['user_id']}


def main():
    dbmod.reset_db()
    fake = FakeSupabase()

    original_is_configured = supabase_auth.is_configured
    original_sign_up = supabase_auth.sign_up
    original_sign_in = supabase_auth.sign_in
    supabase_auth.is_configured = lambda: True
    supabase_auth.sign_up = fake.sign_up
    supabase_auth.sign_in = fake.sign_in

    try:
        # ---------------- signup goes through Supabase ----------------
        client = app.test_client()
        r = client.post('/api/auth/signup', json={
            'display_name': 'Dr Supa', 'email': 'supa.doctor@example.test', 'password': 'whatever-supabase-checks',
        })
        check('signup via Supabase -> 201', r.status_code == 201, (r.status_code, r.get_json()))
        check('signup logs the doctor in immediately',
              client.get('/home').status_code == 200)

        doctor = authmod.get_doctor_by_email('supa.doctor@example.test')
        check('local shadow row was created', doctor is not None)
        check('local shadow row is linked to the Supabase user id',
              doctor is not None and doctor['supabase_user_id'] == 'supabase-user-1')
        check('local password_hash is the sentinel, not a real hash',
              doctor is not None and doctor['password_hash'] == authmod.SUPABASE_MANAGED_PASSWORD_SENTINEL)

        # ---------------- duplicate signup surfaces Supabase's message ----------------
        r = app.test_client().post('/api/auth/signup', json={
            'display_name': 'Dupe', 'email': 'supa.doctor@example.test', 'password': 'anything',
        })
        check('duplicate Supabase signup -> 400', r.status_code == 400, r.status_code)
        check("duplicate signup surfaces Supabase's own message",
              'already registered' in r.get_json().get('message', ''), r.get_json())

        # ---------------- email confirmation required: no session yet ----------------
        orig_sign_up = fake.sign_up
        supabase_auth.sign_up = lambda email, password: orig_sign_up(email, password, confirmed=False)
        r = app.test_client().post('/api/auth/signup', json={
            'display_name': 'Dr Pending', 'email': 'pending.doctor@example.test', 'password': 'whatever',
        })
        supabase_auth.sign_up = fake.sign_up
        check('unconfirmed signup -> CONFIRMATION_REQUIRED, not a session',
              r.get_json().get('status') == 'CONFIRMATION_REQUIRED', r.get_json())
        check('unconfirmed signup response is still 200 (not an error)', r.status_code == 200, r.status_code)

        # ---------------- login goes through Supabase ----------------
        r = app.test_client().post('/api/auth/login', json={
            'email': 'supa.doctor@example.test', 'password': 'whatever-supabase-checks',
        })
        check('login via Supabase -> 200', r.status_code == 200, (r.status_code, r.get_json()))

        r = app.test_client().post('/api/auth/login', json={
            'email': 'supa.doctor@example.test', 'password': 'wrong-password',
        })
        check('wrong password via Supabase -> 401', r.status_code == 401, r.status_code)
        check('failed Supabase login uses the generic message (no detail leak)',
              r.get_json()['message'] == 'Invalid email or password.', r.get_json())

        r = app.test_client().post('/api/auth/login', json={
            'email': 'nobody-knows-this@example.test', 'password': 'whatever',
        })
        check('unknown-to-Supabase account -> 401 with the same generic message',
              r.status_code == 401 and r.get_json()['message'] == 'Invalid email or password.')

        # ---------------- login auto-links a Supabase user with no local row ----------------
        fake.users['preexisting@example.test'] = {'user_id': 'supabase-user-99', 'password': 'pw12345', 'confirmed': True}
        check('no local row exists yet for this Supabase user',
              authmod.get_doctor_by_email('preexisting@example.test') is None)
        r = app.test_client().post('/api/auth/login', json={
            'email': 'preexisting@example.test', 'password': 'pw12345',
        })
        check('login for a Supabase-only user still succeeds -> 200', r.status_code == 200, r.status_code)
        linked = authmod.get_doctor_by_email('preexisting@example.test')
        check('a local shadow row was auto-created on first login',
              linked is not None and linked['supabase_user_id'] == 'supabase-user-99')

        # ---------------- create_doctor_from_supabase links an existing local account ----------------
        local_id = authmod.create_doctor('local.first@example.test', 'a-locally-created-password', 'Dr Local')
        before = authmod.get_doctor(local_id)
        check('locally-created account starts with no supabase_user_id', before['supabase_user_id'] is None)
        linked_id = authmod.create_doctor_from_supabase('local.first@example.test', 'Dr Local', 'supabase-user-later')
        check('create_doctor_from_supabase returns the SAME local id, not a new one', linked_id == local_id)
        after = authmod.get_doctor(local_id)
        check('the existing local row is now linked to the Supabase user id',
              after['supabase_user_id'] == 'supabase-user-later')
        check('exactly one doctor row exists for this email (no duplicate)',
              authmod.get_doctor_by_email('local.first@example.test')['id'] == local_id)

    finally:
        supabase_auth.is_configured = original_is_configured
        supabase_auth.sign_up = original_sign_up
        supabase_auth.sign_in = original_sign_in

    # ---------------- fallback: local auth still works when NOT configured ----------------
    check('supabase_auth.is_configured() is False without env vars', supabase_auth.is_configured() is False)
    r = app.test_client().post('/api/auth/signup', json={
        'display_name': 'Dr Fallback', 'email': 'fallback.doctor@example.test', 'password': 'a-real-local-password',
    })
    check('signup falls back to local auth when Supabase is not configured -> 201',
          r.status_code == 201, (r.status_code, r.get_json()))
    fb = authmod.get_doctor_by_email('fallback.doctor@example.test')
    check('fallback signup stores a real local password hash, not the sentinel',
          fb['password_hash'] != authmod.SUPABASE_MANAGED_PASSWORD_SENTINEL)

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
