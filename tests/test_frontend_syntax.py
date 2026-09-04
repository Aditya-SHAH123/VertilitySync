"""
Parses every <script> block in each Jinja-RENDERED template and fails on a
JavaScript syntax error.

Why rendered rather than raw: the templates embed server values via Jinja
(e.g. `const studyId = {{ study_id | tojson }};`). Opening a template file
directly in a browser leaves those braces unsubstituted and produces
"Uncaught SyntaxError: Unexpected token '{'" - a real symptom, but of
bypassing Flask, not of a code defect. These tests therefore assert two
distinct things:

  1. The rendered pages contain NO leftover Jinja delimiters.
  2. Every rendered script block is syntactically valid JavaScript.

Note this is a PARSE-only check (esprima). It cannot verify runtime behavior:
WebGL/Three.js rendering, the volume-raycasting shader, OrbitControls, and
3D->2D click synchronization still require a real browser to confirm.
"""
import os
import re
import sys

import esprima

os.environ.setdefault('DATABASE_PATH', '/tmp/vitalitysync_test_frontend.db')
# Tests must never touch a real Postgres/Supabase instance, even if
# DATABASE_URL is set in the real environment/.env for production use.
os.environ['DATABASE_URL'] = ''
# Same isolation for the Supabase Auth integration - tests must never
# call the real Supabase API.
os.environ['SUPABASE_URL'] = ''
os.environ['SUPABASE_KEY'] = ''
if os.path.exists(os.environ['DATABASE_PATH']):
    os.remove(os.environ['DATABASE_PATH'])

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'api'))
from index import app  # noqa: E402
import db as dbmod  # noqa: E402
import auth as authmod  # noqa: E402
import cases as casemod  # noqa: E402
import patients as patientsmod  # noqa: E402

FE_EMAIL, FE_PASS = 'frontend.check@example.test', 'frontend-check-password'

PASS = []
FAIL = []

SCRIPT_RE = re.compile(r'<script([^>]*)>(.*?)</script>', re.DOTALL | re.IGNORECASE)
TYPE_RE = re.compile(r'type\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print(f'PASS: {name}')
    else:
        FAIL.append(name)
        print(f'FAIL: {name} {extra}')


def check_page(label, html, expect_scripts=True):
    # 1. No unrendered Jinja delimiters survived into the served page.
    leftover = html.count('{{') + html.count('{%')
    check(f'{label}: no unrendered Jinja delimiters', leftover == 0, f'{leftover} found')

    # 2. Every script block parses. Pages that are intentionally static (the
    #    landing page) carry no script and are not required to.
    blocks = SCRIPT_RE.findall(html)
    if expect_scripts:
        check(f'{label}: contains at least one script block', len(blocks) > 0, len(blocks))

    parsed_count = 0
    for idx, (attrs, body) in enumerate(blocks):
        type_match = TYPE_RE.search(attrs)
        script_type = (type_match.group(1) if type_match else 'text/javascript').lower()

        if script_type == 'importmap':
            import json
            try:
                json.loads(body)
                check(f'{label}: importmap block {idx} is valid JSON', True)
            except json.JSONDecodeError as exc:
                check(f'{label}: importmap block {idx} is valid JSON', False, exc)
            continue

        if not body.strip():
            continue

        is_module = script_type == 'module'
        try:
            if is_module:
                esprima.parseModule(body)
            else:
                esprima.parseScript(body)
            parsed_count += 1
            check(f'{label}: script block {idx} parses ({"module" if is_module else "classic"})', True)
        except esprima.Error as exc:
            check(f'{label}: script block {idx} parses ({"module" if is_module else "classic"})',
                  False, exc)

    return parsed_count


def main():
    # Most templates now live behind authentication, so the client must sign
    # in before their JavaScript can be fetched and parsed at all.
    dbmod.reset_db()
    doctor_id = authmod.create_doctor(FE_EMAIL, FE_PASS, 'Dr Frontend')
    case_row = casemod.create_case(doctor_id, 'Syntax check case')
    patient = patientsmod.create_patient(doctor_id, 'Syntax', 'Check')

    client = app.test_client()
    client.post('/api/auth/login', json={'email': FE_EMAIL, 'password': FE_PASS})

    # The viewer needs a study id in the path; the page renders regardless of
    # whether that study exists (the frontend handles the 404 itself), which
    # is exactly what we want for a static syntax check.
    anon = app.test_client()

    # (label, path, expect_scripts, use_anonymous_client)
    # /login is anonymous-only: a signed-in client is correctly redirected
    # away from it, so it must be fetched without a session.
    pages = [
        ('index.html (/)', '/', True, True),
        ('technology.html (/technology)', '/technology', True, True),
        ('specifications.html (/specifications)', '/specifications', True, True),
        ('safety.html (/safety)', '/safety', True, True),
        ('login.html (/login)', '/login', True, True),
        ('signup.html (/signup)', '/signup', True, True),
        ('cases.html (/cases)', '/cases', True, False),
        ('case_workspace.html (/cases/<ref>)', f'/cases/{case_row["case_ref"]}', True, False),
        ('dashboard.html (/dashboard)', '/dashboard', True, False),
        ('viewer.html (/viewer/<id>)', '/viewer/syntax-check-study-id', True, False),
        ('home.html (/home)', '/home', True, False),
        ('patients.html (/patients)', '/patients', True, False),
        ('patient_workspace.html (/patients/<id>)', f'/patients/{patient["id"]}', True, False),
    ]

    for label, path, expect_scripts, anonymous in pages:
        resp = (anon if anonymous else client).get(path)
        check(f'{label}: renders 200', resp.status_code == 200, resp.status_code)
        if resp.status_code != 200:
            continue
        check_page(label, resp.get_data(as_text=True), expect_scripts=expect_scripts)

    # Explicitly pin the regression from the raw-file screenshot: the viewer's
    # studyId assignments must be real JS string literals after rendering.
    resp = client.get('/viewer/abc-123')
    html = resp.get_data(as_text=True)
    check('viewer: studyId is rendered as a JS string literal',
          html.count('const studyId = "abc-123";') == 2,
          html.count('const studyId = "abc-123";'))

    # Heavy media belongs only on the page that uses it: the detail pages
    # inherit the shared stylesheet but must not carry the hero image or the
    # embedded point cloud, or the split would not have reduced anything.
    anon2 = app.test_client()
    home_kb = len(anon2.get('/').get_data()) / 1024
    for path in ('/technology', '/specifications', '/safety'):
        body = anon2.get(path).get_data(as_text=True)
        kb = len(body) / 1024
        check(f'{path}: does not embed the hero image', 'data:image/webp' not in body)
        check(f'{path}: does not embed the point cloud', 'decodePoints(' not in body)
        check(f'{path}: is far lighter than the home page', kb < home_kb / 3, f'{kb:.0f} KB vs {home_kb:.0f} KB')

    # Every public page must reach the others.
    for path in ('/', '/technology', '/specifications', '/safety'):
        body = anon2.get(path).get_data(as_text=True)
        links = all(h in body for h in ('href="/technology"', 'href="/specifications"', 'href="/safety"'))
        check(f'{path}: links to every other public page', links)

    # The public site must not leak any credential or clinical surface.
    public = app.test_client().get('/').get_data(as_text=True)
    check('public page contains no Higgsfield API key reference',
          'HIGGSFIELD_API_KEY' not in public and 'higgsfield' not in public.lower())
    check('public page makes no external asset requests',
          'https://' not in public.replace('https://vitalitysync', ''),
          [l for l in public.split('\n') if 'https://' in l][:3])

    print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        print('FAILED CHECKS:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
