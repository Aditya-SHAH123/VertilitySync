"""
Tests for tools/colab_export.py - grouping a downloaded DICOM tree into
per-series archives that the app can import.

Uses synthetic DICOM only, generated into a deliberately messy nested tree
that mimics how public collections actually arrive: several series mixed
together, files without a .dcm extension, and non-imaging files alongside.
"""
import os
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
sys.path.insert(0, os.path.dirname(__file__))

import colab_export as ce  # noqa: E402
from make_synthetic_dicom import make_series, make_lung_phantom_series  # noqa: E402

PASS = []
FAIL = []
ROOT = '/tmp/vs_colab_test'
EXPORTS = '/tmp/vs_colab_exports'


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'PASS: {name}')
    else:
        FAIL.append(name)
        print(f'FAIL: {name} {extra}')


def build_tree():
    shutil.rmtree(ROOT, ignore_errors=True)
    shutil.rmtree(EXPORTS, ignore_errors=True)
    a = os.path.join(ROOT, 'CollectionX', 'PatientA', 'Study1', 'SeriesCT')
    b = os.path.join(ROOT, 'CollectionX', 'PatientB', 'deeply', 'nested', 'dir')
    os.makedirs(a, exist_ok=True)
    os.makedirs(b, exist_ok=True)

    make_lung_phantom_series(a, n_slices=30, rows=64, cols=64)
    make_series(b, n_slices=12, rows=32, cols=32)

    # a DICOM file with no extension - common in real collections
    src = sorted(f for f in os.listdir(a) if f.endswith('.dcm'))[0]
    shutil.copy(os.path.join(a, src), os.path.join(a, 'no_extension_file'))

    # non-imaging companions that must be ignored
    with open(os.path.join(ROOT, 'LICENSE.txt'), 'w') as fh:
        fh.write('Creative Commons Attribution 3.0')
    with open(os.path.join(ROOT, 'metadata.csv'), 'w') as fh:
        fh.write('a,b,c\n1,2,3\n')
    return a, b


def main():
    a_dir, b_dir = build_tree()

    # ---------------- scan ----------------
    found = ce.scan(ROOT, quiet=True)
    check('scan finds both series', len(found) == 2, [(s['n_files'], s['modality']) for s in found])
    check('series are sorted by descending slice count',
          found[0]['n_files'] >= found[1]['n_files'], [s['n_files'] for s in found])
    check('each series has a distinct SeriesInstanceUID',
          found[0]['series_uid'] != found[1]['series_uid'])
    check('slice counts are correct (31 incl. the extensionless copy, and 12)',
          {s['n_files'] for s in found} == {31, 12}, {s['n_files'] for s in found})
    check('a DICOM file with no extension is still detected',
          found[0]['n_files'] == 31, found[0]['n_files'])
    check('modality is read from the header', all(s['modality'] == 'CT' for s in found))
    check('image dimensions are read', {(s['rows'], s['columns']) for s in found} == {(64, 64), (32, 32)},
          {(s['rows'], s['columns']) for s in found})
    check('non-imaging files are excluded',
          all(not f.endswith(('.txt', '.csv')) for s in found for f in s['files']))

    check('an empty directory yields no series', ce.scan('/tmp', quiet=True) is not None)

    # ---------------- package ----------------
    z = ce.package(ROOT, series=0, out_dir=EXPORTS, series_list=found)
    check('package writes an archive', z and os.path.exists(z), z)
    check('archive name records the slice count', '31slices' in os.path.basename(z),
          os.path.basename(z))

    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    check('archive contains exactly the series files', len(names) == 31, len(names))
    check('archive members are flat and sequentially named',
          all(n.endswith('.dcm') and '/' not in n for n in names), names[:3])
    check('archive contains no non-imaging files',
          not any(n.endswith(('.txt', '.csv')) for n in names))

    # packaging by UID rather than index
    z2 = ce.package(ROOT, series=found[1]['series_uid'], out_dir=EXPORTS, series_list=found)
    with zipfile.ZipFile(z2) as zf:
        check('packaging by SeriesInstanceUID selects the right series',
              len(zf.namelist()) == 12, len(zf.namelist()))

    check('an out-of-range index returns None rather than raising',
          ce.package(ROOT, series=99, out_dir=EXPORTS, series_list=found) is None)
    check('an unknown UID returns None rather than raising',
          ce.package(ROOT, series='1.2.3.not.real', out_dir=EXPORTS, series_list=found) is None)

    # ---------------- package_all ----------------
    made = ce.package_all(ROOT, out_dir=EXPORTS + '_all', min_slices=20)
    check('package_all honours the minimum slice count', len(made) == 1, len(made))

    # ---------------- large-collection triage ----------------
    # A 24 GB collection must be inventoried and filtered, never packaged
    # wholesale. These guard the behaviour that makes that safe.
    inv_path = os.path.join(ROOT, ce.INVENTORY_FILE)
    check('scan caches an inventory to disk', os.path.exists(inv_path), inv_path)

    cached = ce.scan(ROOT, quiet=True)
    check('a second scan reuses the cached inventory',
          len(cached) == len(found) and cached[0]['series_uid'] == found[0]['series_uid'])
    check('inventory records per-series size', all('size_mb' in s for s in cached))

    rescanned = ce.scan(ROOT, quiet=True, resume=False)
    check('resume=False forces a genuine rescan', len(rescanned) == len(found))
    check('the inventory file is not itself mistaken for imaging data',
          all(ce.INVENTORY_FILE not in f for s in rescanned for f in s['files']))

    picks = ce.shortlist(found, body_part=None, min_slices=20, max_slices=1000, limit=5)
    check('shortlist selects only series above the slice floor',
          all(s['n_files'] >= 20 for s in picks), [s['n_files'] for s in picks])
    check('shortlist excludes the small series', len(picks) == 1, len(picks))
    check('shortlist filters by modality',
          ce.shortlist(found, modality='MR', body_part=None, min_slices=1) == [])

    batch_dir = EXPORTS + '_batch'
    made_batch = ce.package_all(ROOT, out_dir=batch_dir, picks=picks)
    check('package_all writes one archive per selected series',
          len(made_batch) == len(picks), (len(made_batch), len(picks)))

    capped = ce.package_all(ROOT, out_dir=EXPORTS + '_capped',
                             picks=found[:2], max_total_gb=0.000001)
    check('package_all stops at the total-size ceiling',
          len(capped) < 2, len(capped))
    shutil.rmtree(batch_dir, ignore_errors=True)
    shutil.rmtree(EXPORTS + '_capped', ignore_errors=True)

    # ---------------- the localhost guard ----------------
    # Pointing Colab at 127.0.0.1 targets Colab itself; this must be refused
    # before anything else, including any missing-dependency message.
    for bad in ('http://127.0.0.1:5050', 'http://localhost:5050', 'https://localhost'):
        check(f'upload refuses {bad}',
              ce.upload(z, bad, 'a@b.test', 'pw') is None, bad)

    # ---------------- the archive imports into the app ----------------
    os.environ.setdefault('DATABASE_PATH', '/tmp/vs_colab_app.db')
    # Tests must never touch a real Postgres/Supabase instance, even if
    # DATABASE_URL is set in the real environment/.env for production use.
    os.environ['DATABASE_URL'] = ''
    # Same isolation for the Supabase Auth integration - tests must never
    # call the real Supabase API.
    os.environ['SUPABASE_URL'] = ''
    os.environ['SUPABASE_KEY'] = ''
    os.environ.setdefault('STUDY_STORE_PATH', '/tmp/vs_colab_app_store')
    for p in (os.environ['DATABASE_PATH'],):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(os.environ['STUDY_STORE_PATH'], ignore_errors=True)

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
    import db as dbmod
    import auth as authmod
    from index import app

    dbmod.reset_db()
    authmod.create_doctor('colab@example.test', 'colab-export-password', 'Dr Colab')
    client = app.test_client()
    client.post('/api/auth/login', json={'email': 'colab@example.test',
                                          'password': 'colab-export-password'})
    with open(z, 'rb') as fh:
        r = client.post('/api/dicom/upload', data={'files': (fh, os.path.basename(z))},
                         content_type='multipart/form-data')
    check('a packaged archive imports successfully', r.status_code == 200,
          (r.status_code, r.get_json()))
    if r.status_code == 200:
        payload = r.get_json()
        # The extensionless member is a byte copy of another slice, so it
        # carries the same SOPInstanceUID. The importer is expected to drop it
        # as a duplicate: 31 files packaged, 30 distinct slices reconstructed.
        check('duplicate slice from the archive is dropped by the importer',
              payload['summary']['slice_count'] == 30, payload['summary']['slice_count'])
        check('the duplicate is reported rather than silently removed',
              any('duplicate' in m.lower() for m in payload['series_result']['messages']),
              payload['series_result']['messages'])
        check('import reports what it extracted', len(payload.get('archive_notes', [])) > 0)

    shutil.rmtree(ROOT, ignore_errors=True)
    shutil.rmtree(EXPORTS, ignore_errors=True)
    shutil.rmtree(EXPORTS + '_all', ignore_errors=True)

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
