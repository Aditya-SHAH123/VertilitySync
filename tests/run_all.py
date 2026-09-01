"""
Runs every test suite in this directory and reports a combined result.

    python tests/run_all.py

Requires the app dependencies (root requirements.txt) plus the test-only ones
(tests/requirements-test.txt). Synthetic DICOM fixtures are generated
automatically by test_app.py; no real patient data is used anywhere.
"""
import os
import subprocess
import sys

SUITES = [
    'test_lung_segmentation.py',   # unit: rule-based segmentation
    'test_mesh_reconstruction.py', # unit: coordinate transforms + marching cubes
    'test_study_store.py',         # durable, memory-bounded study persistence
    'test_density_regions.py',      # CT densitometry + located regions
    'test_quantitative_analysis.py',  # ground-truth-verified measurements
    'test_app.py',                 # integration: HTTP endpoints, 2D + 3D pipeline
    'test_models.py',              # imaging-relational ORM models (SQLAlchemy)
    'test_asset_storage.py',       # private asset storage: local disk + mocked S3
    'test_measurements_api.py',    # integration: measurements/annotations/ROI/job endpoints
    'test_auth.py',                # authentication, case/study authorization, audit log
    'test_higgsfield_guard.py',    # marketing-media privacy boundary
    'test_colab_export.py',        # remote-tree grouping into importable archives
    'test_frontend_syntax.py',     # rendered-template JS parse check
]

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    results = []
    failed = False

    for suite in SUITES:
        print(f'\n{"=" * 62}\n{suite}\n{"=" * 62}')
        proc = subprocess.run([sys.executable, os.path.join(HERE, suite)],
                              capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)

        summary = next((ln for ln in reversed(proc.stdout.splitlines())
                        if 'passed,' in ln), '(no summary line)')
        results.append((suite, proc.returncode, summary))
        if proc.returncode != 0:
            failed = True

    print(f'\n{"=" * 62}\nSUMMARY\n{"=" * 62}')
    for suite, code, summary in results:
        print(f'{"OK  " if code == 0 else "FAIL"}  {suite:<32} {summary}')

    if failed:
        print('\nSome suites failed.')
        sys.exit(1)
    print('\nAll suites passed.')


if __name__ == '__main__':
    main()
