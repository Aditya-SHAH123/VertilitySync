# ============================================================================
# VitalitySync - Colab export cell.  Paste this whole block into one Colab
# cell and run it.  Nothing else needs to be installed or downloaded.
#
# It inventories your DICOM tree, shows which series are worth importing,
# and writes the ones you choose as .zip archives you can drop on the
# dashboard.  It never uploads anything anywhere.
# ============================================================================
!pip -q install pydicom

import json, os, zipfile, shutil
from collections import defaultdict
import pydicom

# ---- EDIT THIS: where your downloaded data lives in Colab -------------------
DATA_DIR = '/content/data'
EXPORT_DIR = '/content/exports'
# ----------------------------------------------------------------------------

INV = '/content/inventory.json'


def _is_dicom(p):
    try:
        with open(p, 'rb') as f:
            f.seek(128)
            return f.read(4) == b'DICM'
    except OSError:
        return False


def scan(root=DATA_DIR, resume=True):
    """Group every DICOM under `root` by SeriesInstanceUID (headers only)."""
    if resume and os.path.exists(INV):
        inv = json.load(open(INV))
        print(f'Loaded cached inventory: {len(inv)} series. scan(resume=False) to rescan.')
        return summarize(inv)

    series, seen, ndcm = defaultdict(lambda: {'files': [], 'meta': {}, 'bytes': 0}), 0, 0
    for dirpath, _d, files in os.walk(root):
        for fn in files:
            seen += 1
            if seen % 2000 == 0:
                print(f'  ...{seen:,} files, {len(series)} series', flush=True)
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
                    'modality': str(getattr(ds, 'Modality', '?')),
                    'body_part': str(getattr(ds, 'BodyPartExamined', '?')),
                    'rows': int(getattr(ds, 'Rows', 0) or 0),
                    'columns': int(getattr(ds, 'Columns', 0) or 0),
                    'description': str(getattr(ds, 'SeriesDescription', '')),
                }

    inv = [{'series_uid': u, 'n_files': len(e['files']),
            'size_mb': round(e['bytes'] / 1e6, 1),
            'files': sorted(e['files']), **e['meta']} for u, e in series.items()]
    inv.sort(key=lambda s: -s['n_files'])
    json.dump(inv, open(INV, 'w'))
    print(f'\nExamined {seen:,} files, {ndcm:,} DICOM. Inventory cached at {INV}')
    return summarize(inv)


def summarize(inv, top=25):
    total = sum(s['size_mb'] for s in inv)
    print(f'\n{len(inv)} series, {total/1000:.1f} GB total\n')
    print(f"  {'#':>4} {'slices':>7} {'MB':>8}  {'mod':<4} {'body':<10} matrix     description")
    for i, s in enumerate(inv[:top]):
        print(f"  {i:>4} {s['n_files']:>7} {s['size_mb']:>8.1f}  {s['modality']:<4} "
              f"{str(s['body_part'])[:10]:<10} {s['rows']}x{s['columns']:<5} {s['description'][:28]}")
    if len(inv) > top:
        print(f'  ... {len(inv)-top} more')
    print('\nNext:  picks = shortlist(inv)   then   package_all(picks)')
    return inv


def shortlist(inv, modality='CT', body_part='CHEST', min_slices=150, max_slices=600, limit=5):
    """Pick series this app reconstructs well. body_part=None to ignore that tag."""
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
    out = sorted(out, key=lambda s: -s['n_files'])[:limit]
    print(f'{len(out)} series selected:\n')
    for i, s in enumerate(out):
        vol = s['rows'] * s['columns'] * s['n_files'] * 4 / 1e6
        print(f"  [{i}] {s['n_files']:>4} slices  {s['size_mb']:>7.1f} MB  "
              f"-> {vol:>5.0f} MB volume, ~{vol*6.9/1000:.1f} GB RAM to analyse")
    print(f"\nTotal to download: {sum(s['size_mb'] for s in out):.0f} MB")
    return out


def package_all(picks, out_dir=EXPORT_DIR):
    """Write one .zip per selected series."""
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for s in picks:
        name = f"series_{s['series_uid'][-16:]}_{s['n_files']}slices.zip"
        path = os.path.join(out_dir, name)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as zf:
            for i, f in enumerate(s['files']):
                zf.write(f, arcname=f'{i:05d}.dcm')
        made.append(path)
        print(f"  {name}  ({os.path.getsize(path)/1e6:.1f} MB)")
    print(f'\n{len(made)} archive(s) in {out_dir}')
    return made


def to_drive(paths, folder='VitalitySync'):
    """Copy archives to Google Drive - the reliable route for large files."""
    from google.colab import drive
    if not os.path.ismount('/content/drive'):
        drive.mount('/content/drive')
    dest = f'/content/drive/MyDrive/{folder}'
    os.makedirs(dest, exist_ok=True)
    for p in ([paths] if isinstance(paths, str) else paths):
        shutil.copy2(p, dest)
        print(f'  -> {dest}/{os.path.basename(p)}')
    print('\nDownload these from drive.google.com, then drop each on the dashboard.')


def download(paths):
    """Direct browser download. Unreliable above ~200 MB - prefer to_drive()."""
    from google.colab import files
    for p in ([paths] if isinstance(paths, str) else paths):
        mb = os.path.getsize(p) / 1e6
        if mb > 200:
            print(f'  WARNING {os.path.basename(p)} is {mb:.0f} MB - use to_drive() instead')
        files.download(p)


print("""
Ready. Run these one at a time:

    inv   = scan()                 # inventory the tree (cached afterwards)
    picks = shortlist(inv)         # chest CT, 150-600 slices, top 5
    zips  = package_all(picks)     # write the archives
    to_drive(zips)                 # copy to Drive, download from there

If shortlist() returns nothing, the collection probably leaves
BodyPartExamined blank - try:  shortlist(inv, body_part=None)
""")
