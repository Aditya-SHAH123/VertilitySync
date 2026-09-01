"""
Tests for the Higgsfield privacy boundary.

The property under test: no patient/clinical data can reach Higgsfield, and
only reviewed nonclinical prompts may be generated. These tests deliberately
attempt the violations rather than only exercising the happy path.

No network call is made by this suite.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from marketing_media.higgsfield.guard import (  # noqa: E402
    assert_no_phi, validate_approved_prompt, load_approved_prompts,
    assert_not_clinical_source, get_api_key,
    HiggsfieldPrivacyError, HiggsfieldConfigError, REQUIRED_NEGATIVE_PHRASES,
)
from marketing_media.higgsfield.client import build_request, submit_generation  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'PASS: {name}')
    else:
        FAIL.append(name)
        print(f'FAIL: {name} {extra}')


def expect_refusal(name, fn, exc=HiggsfieldPrivacyError):
    try:
        fn()
        check(name, False, 'no exception raised - the boundary did NOT hold')
    except exc:
        check(name, True)
    except Exception as e:  # noqa: BLE001
        check(name, False, f'wrong exception type: {type(e).__name__}: {e}')


def main():
    # ---------------- approved catalog ----------------
    prompts = load_approved_prompts()
    check('approved prompt catalog loads', len(prompts) > 0, len(prompts))
    for pid in prompts:
        try:
            validate_approved_prompt(pid, prompts=prompts)
            check(f'approved prompt "{pid}" passes validation', True)
        except HiggsfieldPrivacyError as e:
            check(f'approved prompt "{pid}" passes validation', False, e)

    for pid, entry in prompts.items():
        low = entry['prompt'].lower()
        check(f'"{pid}" carries required safety phrases',
              all(p in low for p in REQUIRED_NEGATIVE_PHRASES), entry['prompt'][:60])

    # ---------------- free-form prompts are refused ----------------
    expect_refusal('free-form prompt id is refused',
                   lambda: validate_approved_prompt('some_unapproved_prompt'))
    expect_refusal('free-form prompt cannot be submitted',
                   lambda: build_request('not_in_catalog'))

    # ---------------- binary / structured data is refused by type ----------------
    volume = np.zeros((4, 8, 8), dtype=np.float32)
    expect_refusal('numpy imaging volume is refused', lambda: assert_no_phi(volume, 'volume'))
    expect_refusal('raw bytes are refused', lambda: assert_no_phi(b'\x00\x01DICM', 'bytes'))
    expect_refusal('dict payload is refused', lambda: assert_no_phi({'a': 1}, 'dict'))
    expect_refusal('list payload is refused', lambda: assert_no_phi([1, 2, 3], 'list'))
    expect_refusal('array reaches guard via extra_context',
                   lambda: build_request('hero_lung_rotation', extra_context=volume))

    # ---------------- clinical study state is refused ----------------
    fake_study = {'hu_volume': volume, 'summary': {}, 'geometry': None}
    expect_refusal('study state dict is refused', lambda: assert_not_clinical_source(fake_study))
    expect_refusal('array-like object is refused', lambda: assert_not_clinical_source(volume))

    # ---------------- PHI vocabulary is refused ----------------
    phi_strings = [
        'render the patient name on the image',
        'use this DICOM series as reference',
        'include the accession number',
        'based on the radiology report findings',
        'show the diagnosis overlay',
        'patient ID 12345 chest scan',
        'from the .dcm files uploaded',
        'reproduce the clinical note text',
        'date of birth displayed',
        'SOPInstanceUID reference',
    ]
    for s in phi_strings:
        expect_refusal(f'PHI/clinical phrase refused: "{s[:34]}…"',
                       lambda s=s: assert_no_phi(s, 'extra_context'))
        expect_refusal(f'PHI phrase refused through build_request: "{s[:24]}…"',
                       lambda s=s: build_request('hero_lung_rotation', extra_context=s))

    # ---------------- benign styling context is allowed ----------------
    try:
        req = build_request('hero_lung_rotation', extra_context='slightly cooler colour temperature')
        check('benign stylistic context is accepted', 'colour temperature' in req['prompt'])
    except Exception as e:  # noqa: BLE001
        check('benign stylistic context is accepted', False, e)

    # over-long context rejected
    expect_refusal('over-long extra_context is refused',
                   lambda: build_request('hero_lung_rotation', extra_context='x' * 400))

    # ---------------- dry run does not call the API ----------------
    result = submit_generation('hero_lung_rotation', dry_run=True)
    check('dry run returns DRY_RUN status', result['status'] == 'DRY_RUN', result['status'])
    check('dry run states no credit was spent', 'no api call' in result['note'].lower(), result['note'])
    check('dry run payload contains the approved prompt text',
          'no patient identifiers' in result['payload']['prompt'].lower())

    # ---------------- live submission refuses rather than pretending ----------------
    saved = os.environ.pop('HIGGSFIELD_API_KEY', None)
    expect_refusal('missing API key raises a config error (never a fake success)',
                   lambda: submit_generation('hero_lung_rotation', dry_run=False),
                   exc=HiggsfieldConfigError)
    expect_refusal('get_api_key raises when unset', get_api_key, exc=HiggsfieldConfigError)

    os.environ['HIGGSFIELD_API_KEY'] = 'test-key-not-real'
    expect_refusal('live submission refuses (transport intentionally unwired)',
                   lambda: submit_generation('hero_lung_rotation', dry_run=False),
                   exc=HiggsfieldConfigError)
    if saved is None:
        os.environ.pop('HIGGSFIELD_API_KEY', None)
    else:
        os.environ['HIGGSFIELD_API_KEY'] = saved

    # ---------------- key never leaks into a payload ----------------
    os.environ['HIGGSFIELD_API_KEY'] = 'super-secret-value-xyz'
    payload = build_request('hero_lung_rotation')
    check('API key never appears in a built payload',
          'super-secret-value-xyz' not in str(payload), payload)
    os.environ.pop('HIGGSFIELD_API_KEY', None)

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
