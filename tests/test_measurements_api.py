"""
Integration tests for the measurement/annotation/region-of-interest/job HTTP
endpoints added to api/index.py, exercised through the real Flask app + a
throwaway SQLite file for the imaging-relational layer (api/models.py).

Same script style as test_app.py (check()/PASS/FAIL/main()) so run_all.py
picks it up uniformly.
"""

import glob
import os
import sys

# Isolate both databases before importing the app - index.py creates both
# schemas on import.
os.environ.setdefault('DATABASE_PATH', '/tmp/vitalitysync_test_measurements.db')
os.environ.setdefault('IMAGING_DATABASE_URL', 'sqlite:////tmp/imaging_test_measurements.db')
# Without this, index.py's load_dotenv() picks up the real STUDY_STORE_PATH
# from .env (./instance/studies) and this suite's synthetic fixtures land in
# the real study store instead of an isolated directory.
os.environ.setdefault('STUDY_STORE_PATH', '/tmp/vitalitysync_test_measurements_studies')
for _path in ('/tmp/vitalitysync_test_measurements.db', '/tmp/imaging_test_measurements.db'):
    if os.path.exists(_path):
        os.remove(_path)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from index import app  # noqa: E402
import db as dbmod  # noqa: E402
import auth as authmod  # noqa: E402
import measurements as measmod  # noqa: E402
from make_synthetic_dicom import make_lung_phantom_series  # noqa: E402

TEST_EMAIL = 'measurements.doctor@example.test'
TEST_PASSWORD = 'correct-horse-battery-staple'
OTHER_EMAIL = 'other.doctor@example.test'
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


def main():
    make_lung_phantom_series('/tmp/synthetic_dicom_measurements_phantom')
    dbmod.reset_db()
    authmod.create_doctor(TEST_EMAIL, TEST_PASSWORD, 'Dr Test Measurements')
    authmod.create_doctor(OTHER_EMAIL, OTHER_PASSWORD, 'Dr Other')

    client = app.test_client()
    other_client = app.test_client()
    r = client.post('/api/auth/login', json={'email': TEST_EMAIL, 'password': TEST_PASSWORD})
    check('doctor sign in -> 200', r.status_code == 200, r.status_code)
    r = other_client.post('/api/auth/login', json={'email': OTHER_EMAIL, 'password': OTHER_PASSWORD})
    check('other doctor sign in -> 200', r.status_code == 200, r.status_code)

    paths = sorted(glob.glob('/tmp/synthetic_dicom_measurements_phantom/*.dcm'))
    r = client.post('/api/dicom/upload', data={'files': files_field(paths)},
                     content_type='multipart/form-data')
    check('phantom upload -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    study_id = r.get_json()['study_id']
    shape = None

    # ---------------- relational study indexing on import ----------------
    jobs = measmod.list_jobs(study_id)
    check('IMPORT job recorded on upload', any(j['job_type'] == 'IMPORT' for j in jobs), jobs)
    check('IMPORT job completed', all(j['status'] != 'RUNNING' for j in jobs), jobs)

    r = client.get(f'/api/dicom/study/{study_id}/jobs')
    check('jobs endpoint -> 200', r.status_code == 200, r.status_code)
    check('jobs endpoint lists IMPORT job',
          any(j['job_type'] == 'IMPORT' for j in r.get_json()['jobs']))

    # ---------------- point_hu measurement ----------------
    r = client.get(f'/api/dicom/study/{study_id}/summary')
    summary = r.get_json()['summary']
    cols, rows_dim = summary['columns'], summary['rows']
    cx, cy, cz = cols // 2, rows_dim // 2, summary['slice_count'] // 2

    r = client.post(f'/api/dicom/study/{study_id}/measurements',
                     json={'measurement_type': 'point_hu', 'voxels': [[cx, cy, cz]]})
    check('point_hu measurement created -> 201', r.status_code == 201, (r.status_code, r.get_json()))
    payload = r.get_json()
    check('point_hu response has HU units', payload.get('units') == 'HU', payload)
    measurement_id = payload.get('measurement_id')

    r = client.get(f'/api/dicom/study/{study_id}/voxel?x={cx}&y={cy}&z={cz}')
    voxel_hu = r.get_json()['hu']
    check('measured value matches /voxel HU at the same coordinate',
          payload['value'] == voxel_hu, (payload['value'], voxel_hu))

    # ---------------- distance measurement, computed from real geometry ----------------
    r = client.get(f'/api/dicom/study/{study_id}/geometry')
    geometry = r.get_json()['geometry']
    spacing = geometry['spacing_mm']  # [x, y, z] mm per voxel index (col, row, slice)

    r = client.post(f'/api/dicom/study/{study_id}/measurements',
                     json={'measurement_type': 'distance',
                           'voxels': [[0, cy, cz], [10, cy, cz]]})
    check('distance measurement created -> 201', r.status_code == 201, (r.status_code, r.get_json()))
    dist_payload = r.get_json()
    expected_mm = 10 * spacing[0]
    check('distance measurement matches voxel spacing along one axis',
          abs(dist_payload['value'] - expected_mm) < 1e-6,
          (dist_payload['value'], expected_mm))
    check('distance units are mm', dist_payload['units'] == 'mm', dist_payload)

    # ---------------- validation ----------------
    r = client.post(f'/api/dicom/study/{study_id}/measurements',
                     json={'measurement_type': 'point_hu', 'voxels': [[0, 0, 0], [1, 1, 1]]})
    check('point_hu with 2 voxels -> 400', r.status_code == 400, r.status_code)

    r = client.post(f'/api/dicom/study/{study_id}/measurements',
                     json={'measurement_type': 'point_hu', 'voxels': [[999999, 0, 0]]})
    check('out-of-range voxel -> 400', r.status_code == 400, r.status_code)

    r = client.post(f'/api/dicom/study/{study_id}/measurements',
                     json={'measurement_type': 'area', 'voxels': [[0, 0, 0]]})
    check('unsupported measurement_type -> 400', r.status_code == 400, r.status_code)

    # ---------------- listing + ownership isolation ----------------
    r = client.get(f'/api/dicom/study/{study_id}/measurements')
    check('list measurements -> 200', r.status_code == 200, r.status_code)
    check('two measurements listed', len(r.get_json()['measurements']) == 2,
          r.get_json()['measurements'])

    r = other_client.get(f'/api/dicom/study/{study_id}/measurements')
    check("another doctor cannot list this doctor's study measurements -> 404",
          r.status_code == 404, r.status_code)

    # ---------------- delete: ownership + happy path ----------------
    r = client.delete(f'/api/dicom/study/{study_id}/measurements/{measurement_id}')
    check('creating doctor can delete their measurement -> 200', r.status_code == 200, r.status_code)
    r = client.get(f'/api/dicom/study/{study_id}/measurements')
    check('measurement count drops after delete', len(r.get_json()['measurements']) == 1)

    r = client.delete(f'/api/dicom/study/{study_id}/measurements/does-not-exist')
    check('deleting unknown measurement -> 404', r.status_code == 404, r.status_code)

    # ---------------- annotations ----------------
    r = client.post(f'/api/dicom/study/{study_id}/annotations',
                     json={'text': 'Watch this area at the next visit.', 'voxel': [cx, cy, cz]})
    check('annotation with position created -> 201', r.status_code == 201, (r.status_code, r.get_json()))
    annotation_id = r.get_json()['annotation_id']

    r = client.post(f'/api/dicom/study/{study_id}/annotations', json={'text': '   '})
    check('empty annotation text -> 400', r.status_code == 400, r.status_code)

    r = client.get(f'/api/dicom/study/{study_id}/annotations')
    check('list annotations -> 200 with one row', r.status_code == 200 and
          len(r.get_json()['annotations']) == 1, r.get_json())
    check('annotation position round-trips as patient-space mm',
          r.get_json()['annotations'][0]['position_mm'] is not None)

    r = other_client.delete(f'/api/dicom/study/{study_id}/annotations/{annotation_id}')
    check("another doctor's study lookup fails before ownership of the annotation is even checked -> 404",
          r.status_code == 404, r.status_code)
    r = client.delete(f'/api/dicom/study/{study_id}/annotations/{annotation_id}')
    check('creating doctor can delete their annotation -> 200', r.status_code == 200, r.status_code)

    # ---------------- deterministic regions: sync from a real analysis ----------------
    r = client.post(f'/api/dicom/study/{study_id}/segment-lungs')
    check('segmentation runs on phantom -> 200 or 422', r.status_code in (200, 422), r.status_code)
    seg_ok = r.status_code == 200

    r = client.post(f'/api/dicom/study/{study_id}/regions/sync-deterministic')
    check('sync-deterministic before /analysis -> 409', r.status_code == 409, r.status_code)

    r = client.get(f'/api/dicom/study/{study_id}/analysis')
    check('analysis endpoint -> 200', r.status_code == 200, r.status_code)

    r = client.post(f'/api/dicom/study/{study_id}/regions/sync-deterministic')
    check('sync-deterministic after /analysis -> 200', r.status_code == 200, (r.status_code, r.get_json()))

    r = client.get(f'/api/dicom/study/{study_id}/regions?source=deterministic_segmentation')
    check('deterministic regions listing -> 200', r.status_code == 200, r.status_code)
    det_regions = r.get_json()['regions']
    check('every synced region has no clinician label',
          all(reg['label'] is None for reg in det_regions), det_regions)
    check('every synced region has null created_by (not a clinician)',
          all(reg['created_by_doctor_id'] is None for reg in det_regions), det_regions)

    r = client.get(f'/api/dicom/study/{study_id}/regions?source=bogus')
    check('invalid region source -> 400', r.status_code == 400, r.status_code)

    # ---------------- SEGMENTATION job recorded regardless of outcome ----------------
    r = client.get(f'/api/dicom/study/{study_id}/jobs')
    seg_jobs = [j for j in r.get_json()['jobs'] if j['job_type'] == 'SEGMENTATION']
    check('a SEGMENTATION job was recorded', len(seg_jobs) >= 1, seg_jobs)
    if seg_ok:
        check('recorded SEGMENTATION job completed', seg_jobs[0]['status'] == 'COMPLETED', seg_jobs)

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
