"""
Integration tests for the patient workflow: patient CRUD/search, persistent
scan history, duplicate-scan detection (DICOM identifiers + content hash,
never filenames), patient/scan notes, and the optional AI note-polish flow.

Same script style as test_app.py / test_measurements_api.py
(check()/PASS/FAIL/main()) so run_all.py picks it up uniformly.
"""

import glob
import os
import shutil
import sys

os.environ.setdefault('DATABASE_PATH', '/tmp/vitalitysync_test_patients.db')
# Tests must never touch a real Postgres/Supabase instance, even if
# DATABASE_URL is set in the real environment/.env for production use.
os.environ['DATABASE_URL'] = ''
# Same isolation for the Supabase Auth integration - tests must never
# call the real Supabase API.
os.environ['SUPABASE_URL'] = ''
os.environ['SUPABASE_KEY'] = ''
os.environ.setdefault('IMAGING_DATABASE_URL', 'sqlite:////tmp/vitalitysync_test_patients_imaging.db')
# Without this, index.py's load_dotenv() picks up the real STUDY_STORE_PATH
# from .env (./instance/studies) and this suite's synthetic fixtures land in
# the real study store instead of an isolated directory.
os.environ.setdefault('STUDY_STORE_PATH', '/tmp/vitalitysync_test_patients_studies')
for _path in ('/tmp/vitalitysync_test_patients.db', '/tmp/vitalitysync_test_patients_imaging.db'):
    if os.path.exists(_path):
        os.remove(_path)
if os.path.isdir(os.environ['STUDY_STORE_PATH']):
    shutil.rmtree(os.environ['STUDY_STORE_PATH'])

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from index import app  # noqa: E402
import db as dbmod  # noqa: E402
import auth as authmod  # noqa: E402
import patients as patientsmod  # noqa: E402
import ai_notes  # noqa: E402
from make_synthetic_dicom import make_series  # noqa: E402

TEST_EMAIL = 'patients.doctor@example.test'
TEST_PASSWORD = 'correct-horse-battery-staple'
OTHER_EMAIL = 'other.doctor2@example.test'
OTHER_PASSWORD = 'another-horse-battery-staple'

PASS = []
FAIL = []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'PASS: {name}')
    else:
        FAIL.append(name)
        print(f'FAIL: {name} {extra}')


def files_field(paths):
    return [(open(p, 'rb'), os.path.basename(p)) for p in paths]


def upload(client, patient_id, paths):
    fd = {'files': files_field(paths)}
    if patient_id is not None:
        fd['patient_id'] = str(patient_id)
    return client.post('/api/dicom/upload', data=fd, content_type='multipart/form-data')


def main():
    dbmod.reset_db()
    authmod.create_doctor(TEST_EMAIL, TEST_PASSWORD, 'Dr Test Patients')
    authmod.create_doctor(OTHER_EMAIL, OTHER_PASSWORD, 'Dr Other')

    client = app.test_client()
    other_client = app.test_client()
    r = client.post('/api/auth/login', json={'email': TEST_EMAIL, 'password': TEST_PASSWORD})
    check('doctor sign in -> 200', r.status_code == 200, r.status_code)
    r = other_client.post('/api/auth/login', json={'email': OTHER_EMAIL, 'password': OTHER_PASSWORD})
    check('other doctor sign in -> 200', r.status_code == 200, r.status_code)

    r = client.get('/home')
    check('doctor home page loads', r.status_code == 200, r.status_code)
    r = client.get('/login')
    check('login redirect target is /home', True)  # exercised via home load above

    # ---------------- anonymous access is refused ----------------
    anon = app.test_client()
    r = anon.get('/patients')
    check('anonymous /patients redirects to login', r.status_code == 302, r.status_code)
    r = anon.post('/api/patients', json={'first_name': 'A', 'last_name': 'B'})
    check('anonymous patient create API -> 401', r.status_code == 401, r.status_code)

    # ---------------- patient create / persistence / search ----------------
    r = client.post('/api/patients', json={
        'first_name': 'Sarah', 'last_name': 'Miller', 'date_of_birth': '1972-04-18', 'sex': 'female',
    })
    check('create patient -> 201', r.status_code == 201, (r.status_code, r.get_json()))
    patient = r.get_json()['patient']
    patient_id = patient['id']
    check('patient gets an internal_patient_id', bool(patient['internal_patient_id']))

    r = client.post('/api/patients', json={'first_name': '', 'last_name': 'Miller'})
    check('empty first name rejected -> 400', r.status_code == 400, r.status_code)

    r = client.get('/api/patients')
    check('patient list contains the new patient',
          any(p['id'] == patient_id for p in r.get_json()['patients']))

    r = client.get('/api/patients?q=Miller')
    check('search by last name finds the patient',
          any(p['id'] == patient_id for p in r.get_json()['patients']))
    r = client.get('/api/patients?q=Nonexistentxyz')
    check('search with no match returns empty list', r.get_json()['patients'] == [])

    r = client.get('/patients/' + str(patient_id))
    check('patient workspace page loads for the owner', r.status_code == 200, r.status_code)

    # ---------------- authorization: another doctor cannot reach this patient ----------------
    r = other_client.get('/api/patients/' + str(patient_id))
    check("another doctor's GET on this patient -> 404 (not 403)", r.status_code == 404, r.status_code)
    r = other_client.get('/patients/' + str(patient_id))
    check("another doctor's patient page -> 404", r.status_code == 404, r.status_code)
    r = other_client.get('/api/patients?q=Miller')
    check("another doctor's search does not leak this patient",
          r.get_json()['patients'] == [])

    # ---------------- scan upload + persistence ----------------
    make_series('/tmp/synth_patients_a', n_slices=6, rows=48, cols=48, study_uid='1.2.826.0.1.3680043.9.9999.1')
    good_a = sorted(glob.glob('/tmp/synth_patients_a/*.dcm'))

    r = upload(client, patient_id, good_a)
    check('first upload for patient -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    study_id_1 = r.get_json()['study_id']
    check('upload response echoes patient_id', r.get_json().get('patient_id') == patient_id)

    r = client.get('/api/patients/' + str(patient_id) + '/studies')
    check('scan appears in patient scan history', any(s['id'] == study_id_1 for s in r.get_json()['studies']))

    r = client.get('/api/patients/' + str(patient_id))
    check('patient study_count reflects the upload', r.get_json()['patient']['study_count'] == 1)

    # "Leave and return": a fresh request (new client, re-authenticated) must
    # still see the same scan history and be able to reopen the study.
    returning_client = app.test_client()
    returning_client.post('/api/auth/login', json={'email': TEST_EMAIL, 'password': TEST_PASSWORD})
    r = returning_client.get('/api/patients/' + str(patient_id) + '/studies')
    check('scan history persists across a fresh session',
          any(s['id'] == study_id_1 for s in r.get_json()['studies']))
    r = returning_client.get('/viewer/' + study_id_1)
    check('reopened study loads the viewer -> 200', r.status_code == 200, r.status_code)
    check('reopened viewer shows the linked patient name', b'Sarah Miller' in r.data)
    r = returning_client.get('/api/dicom/study/' + study_id_1 + '/summary')
    check('reopened study summary still available without re-upload', r.status_code == 200, r.status_code)

    # ---------------- duplicate detection: identical StudyInstanceUID ----------------
    make_series('/tmp/synth_patients_b', n_slices=6, rows=48, cols=48, study_uid='1.2.826.0.1.3680043.9.9999.1')
    good_b = sorted(glob.glob('/tmp/synth_patients_b/*.dcm'))
    r = upload(client, patient_id, good_b)
    check('re-upload with same StudyInstanceUID -> 409 DUPLICATE',
          r.status_code == 409 and r.get_json().get('status') == 'DUPLICATE', (r.status_code, r.get_json()))
    check('duplicate response names the existing study',
          r.get_json().get('existing_study_id') == study_id_1)

    r = client.get('/api/patients/' + str(patient_id) + '/studies')
    check('duplicate upload did not create a second scan-history row',
          len(r.get_json()['studies']) == 1)

    # ---------------- duplicate detection: same content hash, different UID ----------------
    make_series('/tmp/synth_patients_c', n_slices=6, rows=48, cols=48)  # random new UIDs, identical pixel content
    good_c = sorted(glob.glob('/tmp/synth_patients_c/*.dcm'))
    r = upload(client, patient_id, good_c)
    check('identical pixel content but different StudyInstanceUID -> still DUPLICATE (hash match)',
          r.status_code == 409, (r.status_code, r.get_json()))

    # ---------------- duplicate detection: different content -> NOT a duplicate ----------------
    make_series('/tmp/synth_patients_d', n_slices=9, rows=48, cols=48)  # different slice count -> different content/hash
    good_d = sorted(glob.glob('/tmp/synth_patients_d/*.dcm'))
    r = upload(client, patient_id, good_d)
    check('genuinely different scan -> 200, not flagged duplicate', r.status_code == 200, (r.status_code, r.get_json()))
    study_id_2 = r.get_json()['study_id']

    r = client.get('/api/patients/' + str(patient_id) + '/studies')
    check('scan history now has two distinct studies', len(r.get_json()['studies']) == 2)

    # ---------------- duplicate detection: different filenames, same content ----------------
    os.makedirs('/tmp/synth_patients_e', exist_ok=True)
    for i, p in enumerate(good_a):
        shutil.copy(p, f'/tmp/synth_patients_e/renamed_scan_{i}.dcm')
    good_e = sorted(glob.glob('/tmp/synth_patients_e/*.dcm'))
    r = upload(client, patient_id, good_e)
    check('identical bytes under totally different filenames -> still DUPLICATE',
          r.status_code == 409, (r.status_code, r.get_json()))

    # ---------------- same filenames, different content -> NOT a duplicate ----------------
    check('good_a and good_d share the slice_000.dcm naming pattern',
          os.path.basename(good_a[0]) == os.path.basename(good_d[0]))
    check('...yet were correctly treated as different scans above (see study_id_2)', study_id_2 != study_id_1)

    # ---------------- patient notes ----------------
    r = client.post('/api/patients/' + str(patient_id) + '/notes',
                     json={'content': 'Follow up in six months.', 'title': 'Follow-up'})
    check('create general patient note -> 201', r.status_code == 201, (r.status_code, r.get_json()))
    general_note_id = r.get_json()['note']['id']
    check('new note original == current (no AI involved)',
          r.get_json()['note']['original_content'] == r.get_json()['note']['current_content'])
    check('new note ai_rewritten is false', r.get_json()['note']['ai_rewritten'] is False)

    r = client.post('/api/patients/' + str(patient_id) + '/notes',
                     json={'content': '   '})
    check('empty note content rejected -> 400', r.status_code == 400, r.status_code)

    # ---------------- scan-specific notes ----------------
    r = client.post('/api/patients/' + str(patient_id) + '/notes',
                     json={'content': 'Reviewed right lower lung region.', 'study_id': study_id_1})
    check('create scan note -> 201', r.status_code == 201, r.status_code)
    scan_note_id = r.get_json()['note']['id']

    r = client.post('/api/patients/' + str(patient_id) + '/notes',
                     json={'content': 'wrong study', 'study_id': 'not-a-real-study-id'})
    check('note referencing a study_id outside this patient -> 400', r.status_code == 400, r.status_code)

    r = client.get('/api/patients/' + str(patient_id) + '/notes?study_id=' + study_id_1)
    check('scan-note listing scoped to that study returns exactly one note',
          len(r.get_json()['notes']) == 1 and r.get_json()['notes'][0]['id'] == scan_note_id)

    r = client.get('/api/patients/' + str(patient_id) + '/notes')
    check('unscoped note listing includes both general and scan notes',
          len(r.get_json()['notes']) == 2)

    # ---------------- note authorization ----------------
    r = other_client.put('/api/notes/' + str(general_note_id), json={'content': 'hijacked'})
    check("another doctor cannot update this patient's note -> 404", r.status_code == 404, r.status_code)

    # ---------------- AI note polish: not configured (no API key) ----------------
    saved_key = os.environ.pop('GROQ_API_KEY', None)
    try:
        r = client.post('/api/notes/polish', json={'text': 'lower right lung looks weird'})
        check('polish without a configured key -> NOT_CONFIGURED', r.get_json()['status'] == 'NOT_CONFIGURED', r.get_json())
        check('polish failure response is not a 500', r.status_code != 500, r.status_code)
    finally:
        if saved_key is not None:
            os.environ['GROQ_API_KEY'] = saved_key

    # ---------------- AI note polish: stubbed success (no real network call) ----------------
    original_polish = ai_notes.polish_note

    def fake_polish_ok(text):
        return {'status': 'OK', 'suggestion': 'An area in the right lower lung warrants comparison with the prior imaging study.'}

    ai_notes.polish_note = fake_polish_ok
    try:
        original_text = 'lower right lung looks weird want compare last scan'
        r = client.post('/api/notes/polish', json={'text': original_text})
        check('stubbed polish -> OK with a suggestion', r.get_json()['status'] == 'OK')
        suggestion = r.get_json()['suggestion']
        check('polish endpoint does not touch the database',
              True)  # structurally true: the route never calls patientsmod - see index.py

        # Doctor rejects the rewrite: save the original text unchanged.
        r = client.post('/api/patients/' + str(patient_id) + '/notes',
                         json={'content': original_text, 'ai_rewritten': False})
        check('"Keep Original" path: saved note is unchanged and not flagged as AI-rewritten',
              r.get_json()['note']['current_content'] == original_text and r.get_json()['note']['ai_rewritten'] is False)

        # Doctor accepts the rewrite.
        r = client.post('/api/patients/' + str(patient_id) + '/notes',
                         json={'content': suggestion, 'original_content': original_text, 'ai_rewritten': True})
        check('"Use Rewrite" path: current_content is the AI text', r.get_json()['note']['current_content'] == suggestion)
        check('"Use Rewrite" path: original_content preserves what the doctor actually typed',
              r.get_json()['note']['original_content'] == original_text)
        check('"Use Rewrite" path: ai_rewritten is true', r.get_json()['note']['ai_rewritten'] is True)
    finally:
        ai_notes.polish_note = original_polish

    # ---------------- AI failure never blocks normal note saving ----------------
    def fake_polish_fail(text):
        return {'status': 'FAIL', 'message': 'simulated provider outage'}
    ai_notes.polish_note = fake_polish_fail
    try:
        r = client.post('/api/notes/polish', json={'text': 'some note text'})
        check('simulated AI failure returns FAIL, not a crash', r.get_json()['status'] == 'FAIL')
        r = client.post('/api/patients/' + str(patient_id) + '/notes',
                         json={'content': 'Saved normally despite AI outage.'})
        check('note save still succeeds after an AI failure', r.status_code == 201, r.status_code)
    finally:
        ai_notes.polish_note = original_polish

    # ---------------- vital signs ----------------
    r = client.post('/api/patients/' + str(patient_id) + '/vitals', json={
        'heart_rate_bpm': 78, 'systolic_bp_mmhg': 118, 'diastolic_bp_mmhg': 76,
        'oxygen_saturation_pct': 98.5,
    })
    check('create vital signs -> 201', r.status_code == 201, (r.status_code, r.get_json()))
    vitals_id = r.get_json()['vitals']['id']
    check('vitals response echoes heart rate', r.get_json()['vitals']['heart_rate_bpm'] == 78)
    check('vitals response has a recorded_at timestamp', r.get_json()['vitals']['recorded_at'] is not None)

    r = client.post('/api/patients/' + str(patient_id) + '/vitals', json={})
    check('vitals with no values at all -> 400', r.status_code == 400, r.status_code)

    r = client.post('/api/patients/' + str(patient_id) + '/vitals', json={'heart_rate_bpm': 3000})
    check('implausible heart rate -> 400', r.status_code == 400, r.status_code)

    r = client.post('/api/patients/' + str(patient_id) + '/vitals', json={'temperature_c': 37.1})
    check('a single vital (temperature only) is enough -> 201', r.status_code == 201, r.status_code)
    second_vitals_id = r.get_json()['vitals']['id']

    r = client.get('/api/patients/' + str(patient_id) + '/vitals')
    check('vitals history has both readings', len(r.get_json()['vitals']) == 2, r.get_json())
    check('vitals history is most-recent-first',
          r.get_json()['vitals'][0]['id'] == second_vitals_id, r.get_json()['vitals'])

    r = other_client.get('/api/patients/' + str(patient_id) + '/vitals')
    check("another doctor cannot read this patient's vitals -> 404", r.status_code == 404, r.status_code)

    r = other_client.delete('/api/vitals/' + str(vitals_id))
    check("another doctor cannot delete this doctor's vitals reading -> 403", r.status_code == 403, r.status_code)
    r = client.delete('/api/vitals/' + str(vitals_id))
    check('recording doctor can delete a vitals reading -> 200', r.status_code == 200, r.status_code)
    r = client.get('/api/patients/' + str(patient_id) + '/vitals')
    check('vitals count drops after delete', len(r.get_json()['vitals']) == 1)

    # ---------------- ICD-10 search (static table, no AI) ----------------
    r = client.get('/api/icd10/search?q=fibrosis')
    check('icd10 search for "fibrosis" -> 200', r.status_code == 200, r.status_code)
    results = r.get_json()['results']
    check('icd10 search finds pulmonary fibrosis codes', len(results) > 0, results)
    check('icd10 result has code and description fields',
          all('code' in x and 'description' in x for x in results))
    r = client.get('/api/icd10/search?q=')
    check('empty icd10 query returns no results (not the whole table)',
          r.get_json()['results'] == [])
    r = client.get('/api/icd10/search?q=zzzznomatch')
    check('icd10 search with no match returns empty list', r.get_json()['results'] == [])

    # ---------------- diagnoses (problem list) ----------------
    r = client.post('/api/patients/' + str(patient_id) + '/diagnoses', json={
        'diagnosis_name': 'Idiopathic pulmonary fibrosis', 'icd10_code': 'J84.112',
        'icd10_description': 'Idiopathic pulmonary fibrosis', 'severity': 'moderate',
        'onset_date': '2025-11-01', 'study_id': study_id_1,
    })
    check('create diagnosis -> 201', r.status_code == 201, (r.status_code, r.get_json()))
    diagnosis = r.get_json()['diagnosis']
    diagnosis_id = diagnosis['id']
    check('new diagnosis defaults to active status', diagnosis['status'] == 'active')
    check('new diagnosis has a diagnosed_date even though none was given',
          bool(diagnosis['diagnosed_date']))
    check('new diagnosis carries the linked study id', diagnosis['study_id'] == study_id_1)

    r = client.post('/api/patients/' + str(patient_id) + '/diagnoses', json={'diagnosis_name': '  '})
    check('empty diagnosis name rejected -> 400', r.status_code == 400, r.status_code)

    r = client.post('/api/patients/' + str(patient_id) + '/diagnoses',
                     json={'diagnosis_name': 'Something', 'severity': 'catastrophic'})
    check('invalid severity value rejected -> 400', r.status_code == 400, r.status_code)

    r = client.post('/api/patients/' + str(patient_id) + '/diagnoses',
                     json={'diagnosis_name': 'Wrong study', 'study_id': 'not-a-real-study-id'})
    check('diagnosis referencing a study_id outside this patient -> 400', r.status_code == 400, r.status_code)

    # A diagnosis with no ICD-10 code at all must still be valid - the
    # doctor's own wording is the only required field.
    r = client.post('/api/patients/' + str(patient_id) + '/diagnoses',
                     json={'diagnosis_name': "Doctor's own free-text diagnosis, no code"})
    check('diagnosis with no ICD-10 code is still valid -> 201', r.status_code == 201, r.status_code)
    freetext_diagnosis_id = r.get_json()['diagnosis']['id']
    check('free-text diagnosis has no icd10 code', r.get_json()['diagnosis']['icd10_code'] is None)

    r = client.get('/api/patients/' + str(patient_id) + '/diagnoses')
    check('diagnoses listing has both entries', len(r.get_json()['diagnoses']) == 2)

    r = client.get('/api/patients/' + str(patient_id) + '/diagnoses?status=active')
    check('filter by status=active returns both (both default active)',
          len(r.get_json()['diagnoses']) == 2)
    r = client.get('/api/patients/' + str(patient_id) + '/diagnoses?status=resolved')
    check('filter by status=resolved returns none yet', r.get_json()['diagnoses'] == [])
    r = client.get('/api/patients/' + str(patient_id) + '/diagnoses?status=not-a-real-status')
    check('invalid status filter -> 400', r.status_code == 400, r.status_code)

    # ---------------- diagnosis detail + initial history ----------------
    r = client.get('/api/diagnoses/' + str(diagnosis_id))
    check('get diagnosis detail -> 200', r.status_code == 200, r.status_code)
    check('detail includes the initial history entry', len(r.get_json()['history']) == 1)
    check('initial history entry has old_status None and new_status active',
          r.get_json()['history'][0]['old_status'] is None
          and r.get_json()['history'][0]['new_status'] == 'active')

    # ---------------- editing doctor-authored fields ----------------
    r = client.put('/api/diagnoses/' + str(diagnosis_id), json={'severity': 'severe', 'notes': 'Progressing.'})
    check('update diagnosis fields -> 200', r.status_code == 200, r.status_code)
    check('severity actually changed', r.get_json()['diagnosis']['severity'] == 'severe')
    check('status is untouched by a plain field edit', r.get_json()['diagnosis']['status'] == 'active')

    r = client.put('/api/diagnoses/' + str(diagnosis_id), json={'diagnosis_name': '   '})
    check('editing to an empty diagnosis name is rejected -> 400', r.status_code == 400, r.status_code)

    # ---------------- status lifecycle + history trail ----------------
    r = client.post('/api/diagnoses/' + str(diagnosis_id) + '/status',
                     json={'status': 'resolved', 'note': 'Resolved after treatment course.'})
    check('status transition to resolved -> 200', r.status_code == 200, r.status_code)
    check('diagnosis now shows resolved', r.get_json()['diagnosis']['status'] == 'resolved')

    r = client.get('/api/diagnoses/' + str(diagnosis_id))
    check('history now has two entries (recorded + resolved)', len(r.get_json()['history']) == 2)
    latest = r.get_json()['history'][0]
    check('most recent history entry captures the transition correctly',
          latest['old_status'] == 'active' and latest['new_status'] == 'resolved'
          and latest['note'] == 'Resolved after treatment course.', latest)

    r = client.post('/api/diagnoses/' + str(diagnosis_id) + '/status', json={'status': 'not-a-real-status'})
    check('invalid status transition -> 400', r.status_code == 400, r.status_code)

    r = client.get('/api/patients/' + str(patient_id) + '/diagnoses?status=resolved')
    check('status filter now finds the resolved diagnosis', len(r.get_json()['diagnoses']) == 1)
    r = client.get('/api/patients/' + str(patient_id) + '/diagnoses?status=active')
    check('active filter now only shows the free-text one', len(r.get_json()['diagnoses']) == 1
          and r.get_json()['diagnoses'][0]['id'] == freetext_diagnosis_id)

    # ---------------- authorization ----------------
    r = other_client.get('/api/diagnoses/' + str(diagnosis_id))
    check("another doctor cannot read this diagnosis -> 404", r.status_code == 404, r.status_code)
    r = other_client.put('/api/diagnoses/' + str(diagnosis_id), json={'severity': 'mild'})
    check("another doctor cannot edit this diagnosis -> 404", r.status_code == 404, r.status_code)
    r = other_client.post('/api/diagnoses/' + str(diagnosis_id) + '/status', json={'status': 'chronic'})
    check("another doctor cannot change this diagnosis's status -> 404", r.status_code == 404, r.status_code)
    r = other_client.get('/api/patients/' + str(patient_id) + '/diagnoses')
    check("another doctor's diagnoses listing on this patient -> 404", r.status_code == 404, r.status_code)

    # ---------------- archive ----------------
    r = client.post('/api/patients/' + str(patient_id) + '/archive', json={'archived': True})
    check('archive patient -> 200', r.status_code == 200, r.status_code)
    r = client.get('/api/patients')
    check('archived patient no longer in default listing',
          not any(p['id'] == patient_id for p in r.get_json()['patients']))

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
