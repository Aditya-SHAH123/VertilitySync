import io
import os
import sys
import glob

import base64

# Isolate the test database before importing the app (index.py creates the
# schema on import).
os.environ.setdefault('DATABASE_PATH', '/tmp/vitalitysync_test_app.db')
if os.path.exists(os.environ['DATABASE_PATH']):
    os.remove(os.environ['DATABASE_PATH'])

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from index import app, STUDIES  # noqa: E402
import db as dbmod  # noqa: E402
import auth as authmod  # noqa: E402

import numpy as np
from make_synthetic_dicom import make_series, make_lung_phantom_series  # noqa: E402

TEST_EMAIL = 'imaging.doctor@example.test'
TEST_PASSWORD = 'correct-horse-battery-staple'

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


def setup_fixtures():
    make_series('/tmp/synthetic_dicom_good', n_slices=8, rows=64, cols=64,
                with_geometry=True, with_rescale=True)
    make_series('/tmp/synthetic_dicom_fallback', n_slices=8, rows=64, cols=64,
                with_geometry=False, with_rescale=True)
    make_lung_phantom_series('/tmp/synthetic_dicom_lung_phantom')


def main():
    setup_fixtures()

    # Fresh schema + a single test doctor, then sign in. Every DICOM route is
    # now behind authentication, so the client must carry a session.
    dbmod.reset_db()
    authmod.create_doctor(TEST_EMAIL, TEST_PASSWORD, 'Dr Test Imaging')

    client = app.test_client()

    # --- public page loads without authentication ---
    r = client.get('/')
    check('public home page loads anonymously', r.status_code == 200, r.status_code)
    check('public home page is the marketing site', b'See the scan' in r.data)

    r = client.get('/login')
    check('login page loads anonymously', r.status_code == 200, r.status_code)

    # --- protected pages reject anonymous access ---
    r = client.get('/dashboard')
    check('anonymous /dashboard redirects to login', r.status_code == 302 and '/login' in r.headers.get('Location', ''),
          (r.status_code, r.headers.get('Location')))
    r = client.get('/cases')
    check('anonymous /cases redirects to login', r.status_code == 302, r.status_code)
    r = client.post('/api/dicom/upload', data={})
    check('anonymous upload API -> 401', r.status_code == 401, r.status_code)

    # --- sign in ---
    r = client.post('/api/auth/login', json={'email': TEST_EMAIL, 'password': TEST_PASSWORD})
    check('doctor sign in -> 200', r.status_code == 200, (r.status_code, r.get_json()))

    r = client.get('/dashboard')
    check('dashboard page loads when signed in', r.status_code == 200, r.status_code)
    check('dashboard has upload UI', b'dropzone' in r.data and b'/api/dicom/upload' in r.data)

    # --- upload: no files ---
    r = client.post('/api/dicom/upload', data={})
    check('upload with no files -> 400', r.status_code == 400, r.status_code)

    # --- upload: good series with full geometry ---
    good_paths = sorted(glob.glob('/tmp/synthetic_dicom_good/*.dcm'))
    data = {'files': files_field(good_paths)}
    r = client.post('/api/dicom/upload', data=data, content_type='multipart/form-data')
    check('good series upload -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    payload = r.get_json()
    study_id = payload.get('study_id')
    check('good series returns study_id', bool(study_id))
    summary = payload['summary']
    check('slice_count == 8', summary['slice_count'] == 8, summary['slice_count'])
    check('orientation AVAILABLE (no fallback)', summary['orientation_status'] == 'AVAILABLE', summary['orientation_status'])
    check('HU conversion AVAILABLE', summary['hu_conversion_status'] == 'AVAILABLE', summary['hu_conversion_status'])
    check('series_status PASS', summary['series_status'] == 'PASS', summary['series_status'])

    # --- viewer page ---
    r = client.get(f'/viewer/{study_id}')
    check('viewer page loads for valid study', r.status_code == 200, r.status_code)
    check('viewer page embeds study id', study_id.encode() in r.data)
    check('viewer page shows disabled segmentation/overlay placeholders',
          b'Not yet implemented' in r.data and r.data.count(b'future-btn') >= 4)

    # --- summary endpoint ---
    r = client.get(f'/api/dicom/study/{study_id}/summary')
    check('summary endpoint 200', r.status_code == 200, r.status_code)

    # --- slice endpoints: axial / coronal / sagittal ---
    r = client.get(f'/api/dicom/study/{study_id}/slice/axial/0')
    check('axial slice 0 -> 200', r.status_code == 200, r.status_code)
    axial_json = r.get_json()
    check('axial slice has image data', len(axial_json.get('image_base64', '')) > 100)
    check('axial aspect_ratio == 1.0', axial_json['aspect_ratio'] == 1.0, axial_json['aspect_ratio'])
    check('axial hu_available True', axial_json['hu_available'] is True)

    r = client.get(f'/api/dicom/study/{study_id}/slice/coronal/32')
    check('coronal slice -> 200', r.status_code == 200, r.status_code)
    coronal_json = r.get_json()
    check('coronal aspect_ratio != 1.0 (slice spacing != pixel spacing)',
          abs(coronal_json['aspect_ratio'] - 1.0) > 0.01, coronal_json['aspect_ratio'])

    r = client.get(f'/api/dicom/study/{study_id}/slice/sagittal/32')
    check('sagittal slice -> 200', r.status_code == 200, r.status_code)

    # --- windowing presets ---
    r = client.get(f'/api/dicom/study/{study_id}/slice/axial/0?preset=bone')
    check('bone preset -> 200', r.status_code == 200, r.status_code)
    check('bone preset applies ww/wl', r.get_json()['ww'] == 1800 and r.get_json()['wl'] == 400)

    # --- out of range slice ---
    r = client.get(f'/api/dicom/study/{study_id}/slice/axial/999')
    check('out-of-range axial index -> 400', r.status_code == 400, r.status_code)

    # --- invalid plane ---
    r = client.get(f'/api/dicom/study/{study_id}/slice/diagonal/0')
    check('invalid plane -> 400', r.status_code == 400, r.status_code)

    # --- voxel HU inspection ---
    r = client.get(f'/api/dicom/study/{study_id}/voxel?x=32&y=32&z=4')
    check('voxel endpoint -> 200', r.status_code == 200, r.status_code)
    voxel_json = r.get_json()
    check('voxel HU available', voxel_json['hu_available'] is True)
    check('voxel HU is a plausible number', voxel_json['hu'] is not None)

    # --- voxel out of range ---
    r = client.get(f'/api/dicom/study/{study_id}/voxel?x=9999&y=0&z=0')
    check('voxel out-of-range -> 400', r.status_code == 400, r.status_code)

    # --- unknown study id ---
    r = client.get('/api/dicom/study/does-not-exist/summary')
    check('unknown study summary -> 404', r.status_code == 404, r.status_code)
    r = client.get('/viewer/does-not-exist')
    check('viewer page still renders for unknown study (frontend handles 404)', r.status_code == 200, r.status_code)

    # --- clear/reset study ---
    r = client.delete(f'/api/dicom/study/{study_id}')
    check('clear study -> 200', r.status_code == 200, r.status_code)
    check('study actually removed from memory', study_id not in STUDIES)
    r = client.get(f'/api/dicom/study/{study_id}/summary')
    check('summary after clear -> 404', r.status_code == 404, r.status_code)

    # --- fallback-ordering series (no IOP/IPP) ---
    fb_paths = sorted(glob.glob('/tmp/synthetic_dicom_fallback/*.dcm'))
    data = {'files': files_field(fb_paths)}
    r = client.post('/api/dicom/upload', data=data, content_type='multipart/form-data')
    check('fallback series upload -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    fb_summary = r.get_json()['summary']
    check('fallback series orientation flagged', fb_summary['orientation_status'].startswith('FALLBACK'), fb_summary['orientation_status'])
    check('fallback warning present', any('fallback' in w.lower() for w in fb_summary['validation_warnings']))

    # --- corrupted / non-DICOM file ---
    bad_file = (io.BytesIO(b'this is not a dicom file'), 'bad.dcm')
    r = client.post('/api/dicom/upload', data={'files': [bad_file]}, content_type='multipart/form-data')
    check('all-corrupt upload -> 422', r.status_code == 422, (r.status_code, r.get_json()))
    check('corrupt file reported as FAIL', r.get_json()['per_file_results'][0]['status'] == 'FAIL')

    # --- mixed: 6 good + 1 corrupt -> still succeeds, corrupt one flagged ---
    mixed = files_field(good_paths[:6]) + [(io.BytesIO(b'garbage'), 'bad2.dcm')]
    r = client.post('/api/dicom/upload', data={'files': mixed}, content_type='multipart/form-data')
    check('mixed good+corrupt upload -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    mixed_payload = r.get_json()
    check('mixed upload has 7 per-file results', len(mixed_payload['per_file_results']) == 7, len(mixed_payload['per_file_results']))
    check('mixed upload flags the bad file as FAIL',
          any(f['status'] == 'FAIL' for f in mixed_payload['per_file_results']))
    check('mixed upload volume built from the 6 good slices', mixed_payload['summary']['slice_count'] == 6,
          mixed_payload['summary']['slice_count'])

    # =====================================================================
    # 3D LUNG RECONSTRUCTION PIPELINE
    # =====================================================================

    # --- segmentation refuses an implausible study (disc phantom has no lung-density region) ---
    disc_data = {'files': files_field(good_paths)}
    r = client.post('/api/dicom/upload', data=disc_data, content_type='multipart/form-data')
    disc_study_id = r.get_json()['study_id']
    r = client.post(f'/api/dicom/study/{disc_study_id}/segment-lungs')
    check('segmentation on implausible study -> 422', r.status_code == 422, (r.status_code, r.get_json()))
    check('segmentation on implausible study reports FAIL', r.get_json()['status'] == 'FAIL', r.get_json())

    # --- reconstruct3d before segmentation has run -> 409 ---
    disc_data2 = {'files': files_field(good_paths)}
    r = client.post('/api/dicom/upload', data=disc_data2, content_type='multipart/form-data')
    fresh_study_id = r.get_json()['study_id']
    r = client.get(f'/api/dicom/study/{fresh_study_id}/reconstruct3d')
    check('reconstruct3d before segmentation -> 409', r.status_code == 409, r.status_code)

    # --- geometry endpoint ---
    r = client.get(f'/api/dicom/study/{fresh_study_id}/geometry')
    check('geometry endpoint -> 200', r.status_code == 200, r.status_code)
    geom = r.get_json()['geometry']
    check('geometry reports orientation_reliable', geom['orientation_reliable'] is True, geom)
    check('geometry shape matches upload', geom['shape']['slices'] == 8, geom)

    # --- unknown study geometry -> 404 ---
    r = client.get('/api/dicom/study/does-not-exist/geometry')
    check('geometry for unknown study -> 404', r.status_code == 404, r.status_code)

    # --- happy path: upload the anatomically-informed lung phantom ---
    phantom_paths = sorted(glob.glob('/tmp/synthetic_dicom_lung_phantom/*.dcm'))
    r = client.post('/api/dicom/upload', data={'files': files_field(phantom_paths)},
                     content_type='multipart/form-data')
    check('lung phantom upload -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    phantom_study_id = r.get_json()['study_id']

    r = client.post(f'/api/dicom/study/{phantom_study_id}/segment-lungs')
    check('lung phantom segmentation -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    seg = r.get_json()
    check('lung phantom segmentation succeeds', seg['success'] is True, seg)
    check('lung phantom segmentation method is labeled non-AI',
          'NOT' in seg['method'] and 'AI' in seg['method'], seg['method'])
    check('lung phantom left/right available', seg['left_right_available'] is True, seg)
    check('lung phantom stats include lung_volume_ml', seg['stats'].get('lung_volume_ml', 0) > 0, seg['stats'])

    # --- reconstruct3d: combined, interactive quality ---
    r = client.get(f'/api/dicom/study/{phantom_study_id}/reconstruct3d?quality=interactive&part=combined')
    check('reconstruct3d combined/interactive -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    mesh = r.get_json()
    check('mesh has vertices', mesh['vertex_count'] > 0, mesh['vertex_count'])
    check('mesh has triangles', mesh['triangle_count'] > 0, mesh['triangle_count'])
    verts = np.frombuffer(base64.b64decode(mesh['vertices_b64']), dtype=np.float32).reshape(-1, 3)
    faces = np.frombuffer(base64.b64decode(mesh['faces_b64']), dtype=np.uint32).reshape(-1, 3)
    normals = np.frombuffer(base64.b64decode(mesh['normals_b64']), dtype=np.float32).reshape(-1, 3)
    check('decoded vertex count matches reported count', verts.shape[0] == mesh['vertex_count'], verts.shape)
    check('decoded triangle count matches reported count', faces.shape[0] == mesh['triangle_count'], faces.shape)
    check('decoded normals count matches vertex count', normals.shape[0] == verts.shape[0])
    check('decoded face indices are within vertex range', faces.max() < verts.shape[0])

    # --- reconstruct3d: combined, high_fidelity quality (should have >= vertices than interactive) ---
    r = client.get(f'/api/dicom/study/{phantom_study_id}/reconstruct3d?quality=high_fidelity&part=combined')
    check('reconstruct3d combined/high_fidelity -> 200', r.status_code == 200, r.status_code)
    hf_mesh = r.get_json()
    check('high_fidelity mesh has no downsampling', hf_mesh['downsample_factor'] == 1, hf_mesh['downsample_factor'])
    check('high_fidelity mesh has at least as many vertices as interactive',
          hf_mesh['vertex_count'] >= mesh['vertex_count'], (hf_mesh['vertex_count'], mesh['vertex_count']))

    # --- reconstruct3d: left / right parts ---
    r = client.get(f'/api/dicom/study/{phantom_study_id}/reconstruct3d?quality=interactive&part=left')
    check('reconstruct3d left part -> 200', r.status_code == 200, r.status_code)
    r = client.get(f'/api/dicom/study/{phantom_study_id}/reconstruct3d?quality=interactive&part=right')
    check('reconstruct3d right part -> 200', r.status_code == 200, r.status_code)

    # --- reconstruct3d: invalid quality/part ---
    r = client.get(f'/api/dicom/study/{phantom_study_id}/reconstruct3d?quality=ultra')
    check('reconstruct3d invalid quality -> 400', r.status_code == 400, r.status_code)
    r = client.get(f'/api/dicom/study/{phantom_study_id}/reconstruct3d?part=middle')
    check('reconstruct3d invalid part -> 400', r.status_code == 400, r.status_code)

    # --- volume-texture endpoint ---
    r = client.get(f'/api/dicom/study/{phantom_study_id}/volume-texture?max_dim=48')
    check('volume-texture -> 200', r.status_code == 200, r.status_code)
    vt = r.get_json()
    check('volume-texture respects max_dim cap', max(vt['shape'].values()) <= 48, vt['shape'])
    vt_data = np.frombuffer(base64.b64decode(vt['data_b64']), dtype=np.int16)
    check('volume-texture data size matches reported shape',
          vt_data.size == vt['shape']['slices'] * vt['shape']['rows'] * vt['shape']['cols'])
    check('volume-texture reports a downsample warning when capped', vt['warning'] is not None)

    # --- 2D/3D coordinate sync: a mesh vertex should resolve back to a lung voxel via HU lookup ---
    r = client.get(f'/api/dicom/study/{phantom_study_id}/geometry')
    phantom_geom = r.get_json()['geometry']
    sample_vertex = verts[len(verts) // 2]
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
    from mesh_reconstruction import VolumeGeometry, world_to_voxel_clamped  # noqa: E402
    vg = VolumeGeometry(
        origin_mm=tuple(phantom_geom['origin_mm']), spacing_mm=tuple(phantom_geom['spacing_mm']),
        col_cosines=tuple(phantom_geom['col_cosines']), row_cosines=tuple(phantom_geom['row_cosines']),
        slice_cosines=tuple(phantom_geom['slice_cosines']), orientation_reliable=phantom_geom['orientation_reliable'],
        shape=(phantom_geom['shape']['slices'], phantom_geom['shape']['rows'], phantom_geom['shape']['cols']),
    )
    k, j, i = world_to_voxel_clamped(vg, float(sample_vertex[0]), float(sample_vertex[1]), float(sample_vertex[2]))
    r = client.get(f'/api/dicom/study/{phantom_study_id}/voxel?x={i}&y={j}&z={k}')
    check('mesh vertex resolves to an in-range CT voxel', r.status_code == 200, (r.status_code, k, j, i))
    voxel_json = r.get_json()
    check('mesh-surface voxel has HU available (phantom has full HU coverage)',
          voxel_json['hu_available'] is True, voxel_json)
    check('mesh-surface voxel HU is near the lung/body boundary (not deep background air)',
          voxel_json['hu'] > -1000, voxel_json['hu'])

    # --- ZIP archive import (how public datasets are actually distributed) ---
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for pth in phantom_paths:
            zf.write(pth, arcname='series/' + os.path.basename(pth))
        # non-imaging members a real archive carries; these must be skipped
        zf.writestr('LICENSE.txt', 'Creative Commons Attribution 3.0')
        zf.writestr('manifest.csv', 'a,b,c')
    buf.seek(0)
    r = client.post('/api/dicom/upload', data={'files': (buf, 'series.zip')},
                     content_type='multipart/form-data')
    check('zip archive upload -> 200', r.status_code == 200, (r.status_code, r.get_json()))
    zpay = r.get_json()
    check('zip upload reports what it extracted', len(zpay.get('archive_notes', [])) > 0,
          zpay.get('archive_notes'))
    check('zip upload skipped the non-imaging members',
          any('skipped 2' in n for n in zpay['archive_notes']), zpay['archive_notes'])
    check('zip upload reconstructs the same slice count as loose files',
          zpay['summary']['slice_count'] == 30, zpay['summary']['slice_count'])
    check('zip upload produces a usable study',
          client.get(f"/api/dicom/study/{zpay['study_id']}/summary").status_code == 200)

    # a corrupt archive must be reported, not crash
    r = client.post('/api/dicom/upload',
                     data={'files': (io.BytesIO(b'not a zip at all'), 'broken.zip')},
                     content_type='multipart/form-data')
    check('corrupt zip is rejected cleanly', r.status_code in (400, 422), r.status_code)
    check('corrupt zip explains itself',
          any('not a readable ZIP' in n for n in (r.get_json().get('archive_notes') or [])),
          r.get_json())

    # loose files must still work exactly as before
    r = client.post('/api/dicom/upload', data={'files': files_field(phantom_paths[:8])},
                     content_type='multipart/form-data')
    check('loose-file upload still works alongside zip support', r.status_code == 200, r.status_code)

    # --- quantitative analysis endpoint ---
    r = client.get(f'/api/dicom/study/{phantom_study_id}/analysis')
    check('analysis endpoint -> 200', r.status_code == 200, r.status_code)
    apay = r.get_json()
    an = apay['analysis']
    check('analysis is computed on first request', apay['cached'] is False, apay['cached'])
    check('analysis reports lung volume', an['lung_metrics']['total_lung_volume_ml'] > 0,
          an['lung_metrics'])
    check('analysis volume agrees with the segmentation stat',
          abs(an['lung_metrics']['total_lung_volume_ml'] - seg['stats']['lung_volume_ml']) < 1.0,
          (an['lung_metrics']['total_lung_volume_ml'], seg['stats']['lung_volume_ml']))
    check('analysis includes density statistics',
          an['density_metrics']['whole_lungs']['status'] == 'OK')
    check('analysis includes a histogram', an['histogram']['status'] == 'OK')
    check('analysis includes regional zones', an['regional_metrics']['zones']['status'] == 'OK')
    check('analysis includes pleural bands', an['regional_metrics']['pleural_bands']['status'] == 'OK')
    check('analysis includes scan quality', an['scan_quality']['status'] in ('OK', 'WARNING', 'FAILED'))
    check('lobes are NOT_AVAILABLE, not fabricated', an['lobe_metrics']['status'] == 'NOT_AVAILABLE')
    check('findings are NOT_AVAILABLE with an empty region list',
          an['findings']['status'] == 'NOT_AVAILABLE' and an['findings']['regions'] == [])
    check('analysis declares that no model contributed',
          an['provenance_summary']['model_derived_values'] == 'none')

    r = client.get(f'/api/dicom/study/{phantom_study_id}/analysis')
    check('analysis is cached on the second request', r.get_json()['cached'] is True)
    r = client.get(f'/api/dicom/study/{phantom_study_id}/analysis?refresh=1')
    check('refresh recomputes the analysis', r.get_json()['cached'] is False)

    # a study without segmentation must still return quality, and nothing else
    r = client.post('/api/dicom/upload', data={'files': files_field(phantom_paths)},
                     content_type='multipart/form-data')
    unseg_id = r.get_json()['study_id']
    r = client.get(f'/api/dicom/study/{unseg_id}/analysis')
    check('analysis without segmentation -> 200', r.status_code == 200, r.status_code)
    un = r.get_json()['analysis']
    check('unsegmented study still reports scan quality',
          un['scan_quality']['status'] in ('OK', 'WARNING', 'FAILED'))
    check('unsegmented study reports no lung volume',
          un['lung_metrics']['status'] == 'NOT_AVAILABLE', un['lung_metrics'])

    r = client.get('/api/dicom/study/does-not-exist/analysis')
    check('analysis for unknown study -> 404', r.status_code == 404, r.status_code)

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
