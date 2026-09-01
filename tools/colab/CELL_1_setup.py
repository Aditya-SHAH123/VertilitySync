# ═══════════════════════════════════════════════════════════════════════════
#  CELL 1 of 5  —  SETUP + FIND YOUR DATA
#  Paste this whole block into a Colab cell and run it.
#  Nothing to edit. It locates your DICOM data by itself.
# ═══════════════════════════════════════════════════════════════════════════
!pip -q install pydicom

import json, os, shutil, zipfile
from collections import defaultdict
import pydicom

EXPORT_DIR = '/content/exports'
INV_PATH   = '/content/inventory.json'
DATA_DIR   = None          # set automatically by find_data() below


def _is_dicom(p):
    """DICOM files carry 'DICM' at byte 128. Many collections store files with
    no extension at all, so the extension alone is not a reliable test."""
    try:
        with open(p, 'rb') as f:
            f.seek(128)
            return f.read(4) == b'DICM'
    except OSError:
        return False


def find_data(search_root='/content', max_probe=40):
    """Locate the directory holding your DICOM data.

    Probes a bounded number of files per candidate rather than walking the
    whole tree, so this stays fast even on a very large collection.
    """
    global DATA_DIR
    skip = {'/content/drive', '/content/sample_data', '/content/exports', '/content/.config'}
    candidates = []

    for entry in sorted(os.scandir(search_root), key=lambda e: e.name):
        if not entry.is_dir() or entry.path in skip or entry.name.startswith('.'):
            continue
        n_files = n_dicom = 0
        for dirpath, _d, files in os.walk(entry.path):
            for fn in files:
                n_files += 1
                if n_dicom < max_probe:
                    p = os.path.join(dirpath, fn)
                    if fn.lower().endswith(('.dcm', '.dicom')) or _is_dicom(p):
                        n_dicom += 1
            if n_files > 20000 and n_dicom >= max_probe:
                break            # enough evidence; stop walking
        if n_dicom:
            candidates.append((n_dicom, n_files, entry.path))

    if not candidates:
        print('No DICOM data found under', search_root)
        print('\nList what is there and set DATA_DIR yourself:')
        print("    !ls -la /content")
        print("    DATA_DIR = '/content/your_folder'")
        return None

    candidates.sort(key=lambda c: -c[1])
    print('DICOM data found in:\n')
    for n_dcm, n_files, path in candidates:
        print(f'  {path}   ({n_files:,} files)')
    DATA_DIR = candidates[0][2]
    print(f'\nUsing:  DATA_DIR = {DATA_DIR!r}')
    if len(candidates) > 1:
        print('If that is the wrong folder, set DATA_DIR yourself before Cell 2.')
    return DATA_DIR


# ---------------------------------------------------------------------------

def scan(root=None, resume=True):
    """Group every DICOM file under `root` by SeriesInstanceUID.

    Reads headers only, so a large collection is inventoried without decoding
    pixel data. The result is cached to INV_PATH; a later call reloads it
    instead of rescanning.
    """
    root = root or DATA_DIR
    if not root:
        print('DATA_DIR is not set. Run find_data() first (Cell 1).')
        return None

    if resume and os.path.exists(INV_PATH):
        inv = json.load(open(INV_PATH))
        print(f'Loaded cached inventory ({len(inv)} series). scan(resume=False) to rescan.')
        return summarize(inv)

    series, seen, ndcm = defaultdict(lambda: {'files': [], 'meta': {}, 'bytes': 0}), 0, 0
    for dirpath, _d, files in os.walk(root):
        for fn in files:
            seen += 1
            if seen % 2000 == 0:
                print(f'  ...{seen:,} files examined, {len(series)} series so far', flush=True)
            p = os.path.join(dirpath, fn)
            if not (fn.lower().endswith(('.dcm', '.dicom')) or _is_dicom(p)):
                continue
            try:
                ds = pydicom.dcmread(p, stop_before_pixels=True, force=True)
                uid = getattr(ds, 'SeriesInstanceUID', None)
                if not uid:
                    continue
            except Exception:
                continue
            ndcm += 1
            e = series[uid]
            e['files'].append(p)
            try:
                e['bytes'] += os.path.getsize(p)
            except OSError:
                pass
            if not e['meta']:
                e['meta'] = {
                    'modality':    str(getattr(ds, 'Modality', '?')),
                    'body_part':   str(getattr(ds, 'BodyPartExamined', '?')),
                    'rows':        int(getattr(ds, 'Rows', 0) or 0),
                    'columns':     int(getattr(ds, 'Columns', 0) or 0),
                    'description': str(getattr(ds, 'SeriesDescription', '')),
                }

    inv = [{'series_uid': u, 'n_files': len(e['files']),
            'size_mb': round(e['bytes'] / 1e6, 1),
            'files': sorted(e['files']), **e['meta']} for u, e in series.items()]
    inv.sort(key=lambda s: -s['n_files'])
    json.dump(inv, open(INV_PATH, 'w'))
    print(f'\nExamined {seen:,} files, {ndcm:,} DICOM. Inventory cached.')
    return summarize(inv)


def summarize(inv, top=25):
    total = sum(s['size_mb'] for s in inv)
    print(f'\n{len(inv)} series, {total/1000:.1f} GB total\n')
    print(f"  {'#':>4} {'slices':>7} {'MB':>8}  {'mod':<4} {'body':<10} {'matrix':<11} description")
    for i, s in enumerate(inv[:top]):
        print(f"  {i:>4} {s['n_files']:>7} {s['size_mb']:>8.1f}  {s['modality']:<4} "
              f"{str(s['body_part'])[:10]:<10} {str(s['rows'])+'x'+str(s['columns']):<11} "
              f"{s['description'][:26]}")
    if len(inv) > top:
        print(f'  ... and {len(inv)-top} more')
    return inv


def shortlist(inv, modality='CT', body_part='CHEST',
              min_slices=150, max_slices=600, limit=5, _relaxed=False):
    """Select the series worth importing.

    Automatically relaxes the body-part filter when it excludes everything,
    because many collections leave BodyPartExamined blank.
    """
    if not inv:
        print('No inventory. Run Cell 2 first.')
        return []

    out = []
    for s in inv:
        if modality and str(s['modality']).upper() != modality.upper():
            continue
        bp = str(s['body_part']).upper()
        if body_part and body_part.upper() not in bp and bp not in ('', '?', 'NONE'):
            continue
        if not (min_slices <= s['n_files'] <= max_slices):
            continue
        out.append(s)

    if not out and not _relaxed:
        print('Nothing matched. Retrying without the body-part filter '
              '(this collection likely leaves BodyPartExamined blank)...\n')
        return shortlist(inv, modality=modality, body_part=None,
                         min_slices=min_slices, max_slices=max_slices,
                         limit=limit, _relaxed=True)

    if not out:
        biggest = inv[0]['n_files'] if inv else 0
        print(f'Still nothing. The largest series has {biggest} slices.')
        print(f'Try:  picks = shortlist(inv, modality=None, body_part=None, min_slices=40)')
        return []

    out = sorted(out, key=lambda s: -s['n_files'])[:limit]
    print(f'{len(out)} series selected:\n')
    for i, s in enumerate(out):
        vol = s['rows'] * s['columns'] * s['n_files'] * 4 / 1e6
        print(f"  [{i}] {s['n_files']:>4} slices  {s['size_mb']:>7.1f} MB on disk  "
              f"->  {vol:>5.0f} MB volume, ~{vol*6.9/1000:.1f} GB RAM to analyse")
    print(f"\nTotal to move: {sum(s['size_mb'] for s in out):.0f} MB "
          f"(not the whole collection).")
    return out


def package_all(picks, out_dir=EXPORT_DIR):
    """Write one .zip per selected series, ready for the dashboard."""
    if not picks:
        print('Nothing selected. Run Cell 3 first.')
        return []
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for s in picks:
        name = f"series_{s['series_uid'][-16:]}_{s['n_files']}slices.zip"
        path = os.path.join(out_dir, name)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as zf:
            for i, f in enumerate(s['files']):
                zf.write(f, arcname=f'{i:05d}.dcm')
        made.append(path)
        print(f"  {name}   ({os.path.getsize(path)/1e6:.1f} MB)")
    print(f'\n{len(made)} archive(s) written to {out_dir}')
    return made


def to_drive(paths, folder='VitalitySync'):
    """Copy archives to Google Drive - the reliable route for large files."""
    if not paths:
        print('Nothing to copy. Run Cell 4 first.')
        return
    from google.colab import drive
    if not os.path.ismount('/content/drive'):
        drive.mount('/content/drive')
    dest = f'/content/drive/MyDrive/{folder}'
    os.makedirs(dest, exist_ok=True)
    for p in ([paths] if isinstance(paths, str) else paths):
        shutil.copy2(p, dest)
        print(f'  -> {dest}/{os.path.basename(p)}')
    print(f'\nDone. Open drive.google.com, download from the "{folder}" folder,')
    print('then drop each .zip on the VitalitySync dashboard.')


find_data()
print('\nSetup complete. Run CELL 2 next.')
