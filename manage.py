"""
Administrative CLI for VitalitySync.

    python manage.py create-doctor <email> <display name>
    python manage.py list-doctors
    python manage.py seed-demo <email>          # demo CASE records for an account
    python manage.py audit [limit]

Passwords are prompted for interactively and never accepted as a command-line
argument (which would leak them into shell history and the process list).
There are no default or hardcoded accounts anywhere in this project - an
administrator must create the first doctor with this command.
"""

import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api'))

import db as dbmod          # noqa: E402
import auth as authmod      # noqa: E402
import cases as casemod     # noqa: E402


def cmd_create_doctor(email, display_name):
    dbmod.init_db()
    pw1 = getpass.getpass('Password (min 12 chars): ')
    pw2 = getpass.getpass('Confirm password: ')
    if pw1 != pw2:
        print('Passwords do not match.')
        sys.exit(1)
    try:
        doctor_id = authmod.create_doctor(email, pw1, display_name)
    except ValueError as exc:
        print(f'Error: {exc}')
        sys.exit(1)
    print(f'Created doctor #{doctor_id}: {display_name} <{email}>')


def cmd_list_doctors():
    dbmod.init_db()
    rows = dbmod.get_db().execute(
        'SELECT id, email, display_name, created_at FROM doctors ORDER BY id').fetchall()
    if not rows:
        print('No doctor accounts exist yet. Create one with: python manage.py create-doctor <email> <name>')
        return
    for r in rows:
        print(f"#{r['id']:<4} {r['display_name']:<24} {r['email']:<34} {r['created_at'][:10]}")


def cmd_seed_demo(email):
    """Creates clearly-labelled DEMO case records for an existing account.

    These are empty investigation containers only - they contain no imaging
    and no patient data of any kind.
    """
    dbmod.init_db()
    row = dbmod.get_db().execute('SELECT id FROM doctors WHERE email = ?',
                                  (email.strip().lower(),)).fetchone()
    if row is None:
        print(f'No account found for {email}. Create it first.')
        sys.exit(1)
    samples = [
        ('Demo — chest CT, baseline interval', 'needs_review'),
        ('Demo — chest CT, reconstruction review', 'ready'),
        ('Demo — chest CT, import in progress', 'processing'),
    ]
    for title, status in samples:
        c = casemod.create_case(row['id'], title, status=status, is_demo=True)
        print(f"Created {c['case_ref']}  [{status}]  {title}")
    print('\nAll seeded records are flagged is_demo=1 and render with a "DEMO RECORD" tag.')


def cmd_audit(limit=40):
    dbmod.init_db()
    rows = dbmod.get_db().execute(
        'SELECT ts, doctor_id, event, target_type, target_id, outcome FROM audit_log '
        'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    if not rows:
        print('Audit log is empty.')
        return
    print(f"{'TIMESTAMP':<28} {'DR':<4} {'EVENT':<26} {'TARGET':<28} OUTCOME")
    for r in rows:
        target = f"{r['target_type'] or ''}:{r['target_id'] or ''}".strip(':')
        print(f"{r['ts'][:26]:<28} {str(r['doctor_id'] or '-'):<4} {r['event']:<26} {target:<28} {r['outcome']}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == 'create-doctor':
        if len(sys.argv) < 4:
            print('usage: manage.py create-doctor <email> <display name>')
            sys.exit(1)
        cmd_create_doctor(sys.argv[2], ' '.join(sys.argv[3:]))
    elif cmd == 'list-doctors':
        cmd_list_doctors()
    elif cmd == 'seed-demo':
        if len(sys.argv) < 3:
            print('usage: manage.py seed-demo <email>')
            sys.exit(1)
        cmd_seed_demo(sys.argv[2])
    elif cmd == 'audit':
        cmd_audit(int(sys.argv[2]) if len(sys.argv) > 2 else 40)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()
