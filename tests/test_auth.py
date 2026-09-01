"""
Authentication and authorization tests.

Covers the security properties that matter most:
  * passwords are hashed, never stored or comparable in plaintext
  * anonymous requests are rejected by protected pages AND protected APIs
  * a signed-in doctor cannot reach another doctor's case by editing the URL
  * a signed-in doctor cannot reach another doctor's imaging study by id
  * denied and non-existent resources are indistinguishable to the caller
  * logout actually ends the session
  * security-relevant events reach the audit log without clinical content
"""
import os
import sys

os.environ.setdefault('DATABASE_PATH', '/tmp/vitalitysync_test_auth.db')
if os.path.exists(os.environ['DATABASE_PATH']):
    os.remove(os.environ['DATABASE_PATH'])

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from index import app, STUDIES  # noqa: E402
import db as dbmod  # noqa: E402
import auth as authmod  # noqa: E402
import cases as casemod  # noqa: E402

PASS = []
FAIL = []

A_EMAIL, A_PASS = 'alice.doctor@example.test', 'alice-long-password-1'
B_EMAIL, B_PASS = 'bob.doctor@example.test', 'bob-long-password-22'


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'PASS: {name}')
    else:
        FAIL.append(name)
        print(f'FAIL: {name} {extra}')


def sign_in(client, email, password):
    return client.post('/api/auth/login', json={'email': email, 'password': password})


def main():
    dbmod.reset_db()
    STUDIES.clear()

    alice_id = authmod.create_doctor(A_EMAIL, A_PASS, 'Dr Alice')
    bob_id = authmod.create_doctor(B_EMAIL, B_PASS, 'Dr Bob')

    # ---------------- password storage ----------------
    conn = dbmod.get_db()
    row = conn.execute('SELECT password_hash FROM doctors WHERE id = ?', (alice_id,)).fetchone()
    check('password is not stored in plaintext', A_PASS not in row['password_hash'], 'plaintext leak!')
    check('password hash uses a recognised algorithm',
          row['password_hash'].split(':')[0] in ('pbkdf2', 'scrypt'), row['password_hash'][:24])

    # ---------------- password policy ----------------
    try:
        authmod.create_doctor('short@example.test', 'tooshort', 'Dr Short')
        check('short password is rejected', False)
    except ValueError:
        check('short password is rejected', True)
    try:
        authmod.create_doctor(A_EMAIL, 'another-long-password', 'Duplicate')
        check('duplicate email is rejected', False)
    except ValueError:
        check('duplicate email is rejected', True)

    # ---------------- credential verification ----------------
    check('correct credentials verify', authmod.verify_credentials(A_EMAIL, A_PASS) is not None)
    check('wrong password fails', authmod.verify_credentials(A_EMAIL, 'wrong-password-here') is None)
    check('unknown account fails', authmod.verify_credentials('nobody@example.test', A_PASS) is None)

    # ---------------- anonymous access ----------------
    anon = app.test_client()
    for path in ('/dashboard', '/cases'):
        r = anon.get(path)
        check(f'anonymous {path} -> redirect to login',
              r.status_code == 302 and '/login' in r.headers.get('Location', ''), r.status_code)
    for path in ('/api/cases', '/api/dicom/study/x/summary'):
        r = anon.get(path)
        check(f'anonymous API {path} -> 401', r.status_code == 401, r.status_code)
    r = anon.get('/')
    check('public home stays open to anonymous visitors', r.status_code == 200, r.status_code)

    # ---------------- sign in ----------------
    alice = app.test_client()
    r = sign_in(alice, A_EMAIL, A_PASS)
    check('alice signs in -> 200', r.status_code == 200, (r.status_code, r.get_json()))

    bad = app.test_client()
    r = sign_in(bad, A_EMAIL, 'definitely-not-the-password')
    check('wrong password -> 401', r.status_code == 401, r.status_code)
    check('failed login message does not reveal whether the account exists',
          r.get_json()['message'] == 'Invalid email or password.', r.get_json())
    r = sign_in(bad, 'ghost@example.test', 'definitely-not-the-password')
    check('unknown account returns the same message',
          r.get_json()['message'] == 'Invalid email or password.', r.get_json())

    r = alice.get('/cases')
    check('signed-in doctor reaches /cases', r.status_code == 200, r.status_code)

    # ---------------- case authorization ----------------
    alice_case = casemod.create_case(alice_id, 'Alice private case')
    bob_case = casemod.create_case(bob_id, 'Bob private case')

    r = alice.get(f'/cases/{alice_case["case_ref"]}')
    check('alice opens her own case', r.status_code == 200, r.status_code)

    r = alice.get(f'/cases/{bob_case["case_ref"]}')
    check("alice CANNOT open bob's case by URL (404)", r.status_code == 404, r.status_code)

    r = alice.get(f'/api/cases/{bob_case["case_ref"]}')
    check("alice CANNOT read bob's case via API (404)", r.status_code == 404, r.status_code)

    r_missing = alice.get('/api/cases/CASE-DOESNOTEXIST')
    check('nonexistent case and forbidden case are indistinguishable',
          r_missing.status_code == r.status_code, (r_missing.status_code, r.status_code))

    r = alice.post(f'/api/cases/{bob_case["case_ref"]}/notes', json={'content': 'unauthorized note'})
    check("alice CANNOT add a note to bob's case", r.status_code == 404, r.status_code)

    r = alice.post(f'/api/cases/{bob_case["case_ref"]}/status', json={'status': 'archived'})
    check("alice CANNOT change status on bob's case", r.status_code == 404, r.status_code)

    # list isolation
    r = alice.get('/api/cases')
    refs = [c['case_ref'] for c in r.get_json()['cases']]
    check('case list contains own case', alice_case['case_ref'] in refs, refs)
    check("case list excludes another doctor's case", bob_case['case_ref'] not in refs, refs)

    # authorization helper directly
    check('doctor_can_access_case true for owner',
          authmod.doctor_can_access_case(alice_id, alice_case['id']))
    check('doctor_can_access_case false for non-owner',
          not authmod.doctor_can_access_case(alice_id, bob_case['id']))

    # explicit sharing works
    casemod.grant_access(bob_case['id'], alice_id, role='viewer')
    check('explicit grant enables access',
          authmod.doctor_can_access_case(alice_id, bob_case['id']))
    r = alice.get(f'/api/cases/{bob_case["case_ref"]}')
    check('shared case becomes readable after grant', r.status_code == 200, r.status_code)

    # ---------------- notes ----------------
    r = alice.post(f'/api/cases/{alice_case["case_ref"]}/notes', json={'content': 'Baseline observation.'})
    check('note created on own case -> 201', r.status_code == 201, r.status_code)
    r = alice.post(f'/api/cases/{alice_case["case_ref"]}/notes', json={'content': '   '})
    check('empty note rejected -> 400', r.status_code == 400, r.status_code)
    r = alice.get(f'/api/cases/{alice_case["case_ref"]}')
    check('note is returned with the case', len(r.get_json()['notes']) == 1, r.get_json()['notes'])

    # ---------------- imaging study ownership ----------------
    STUDIES['study-owned-by-bob'] = {
        'hu_volume': None, 'hu_available_per_slice': [True], 'pixel_spacing': [1, 1],
        'slice_spacing': 1, 'summary': {}, 'geometry': None, 'segmentation': None,
        'owner_doctor_id': bob_id, 'created_at': 'now',
    }
    r = alice.get('/api/dicom/study/study-owned-by-bob/summary')
    check("alice CANNOT read bob's study summary (404)", r.status_code == 404, r.status_code)
    r = alice.delete('/api/dicom/study/study-owned-by-bob')
    check("alice CANNOT delete bob's study (404)", r.status_code == 404, r.status_code)
    check('bob\'s study still exists after alice\'s delete attempt',
          'study-owned-by-bob' in STUDIES)
    r = alice.get('/viewer/study-owned-by-bob')
    check("alice CANNOT open bob's study in the viewer (404)", r.status_code == 404, r.status_code)

    # ---------------- session lifecycle ----------------
    r = alice.get('/api/session')
    check('session endpoint reports authenticated', r.get_json()['authenticated'] is True)
    r = alice.post('/api/auth/logout')
    check('logout -> 200', r.status_code == 200, r.status_code)
    r = alice.get('/api/session')
    check('session endpoint reports anonymous after logout', r.get_json()['authenticated'] is False)
    r = alice.get('/cases')
    check('protected page rejects the logged-out session', r.status_code == 302, r.status_code)

    # ---------------- audit log ----------------
    rows = dbmod.get_db().execute('SELECT event, outcome FROM audit_log').fetchall()
    events = [r['event'] for r in rows]
    for expected in ('login', 'login_failed', 'logout', 'case_accessed', 'case_access_denied', 'note_created'):
        check(f'audit log records "{expected}"', expected in events, events)

    cols = [d[1] for d in dbmod.get_db().execute("PRAGMA table_info(audit_log)").fetchall()]
    check('audit log has no column for clinical content',
          not any(c in cols for c in ('content', 'note_text', 'body', 'pixel_data')), cols)

    note_row = dbmod.get_db().execute("SELECT target_id FROM audit_log WHERE event = 'note_created'").fetchone()
    check('audit log stores the note id, not the note text',
          note_row['target_id'].isdigit(), note_row['target_id'])

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
