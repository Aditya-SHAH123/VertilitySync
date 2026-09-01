"""
Package DICOM data held in Google Colab (or any remote machine) into
per-series ZIP archives that VitalitySync can import directly.

WHY THIS EXISTS
    A downloaded collection is usually a deep directory tree of loose .dcm
    files from many studies mixed together. The app imports ONE SERIES at a
    time - a single series is what forms a single volume - so the useful step
    is to group files by SeriesInstanceUID and write one archive per series.

    Colab cannot reach an application running on your own laptop
    (127.0.0.1 there is Colab's own loopback, not yours). The default and
    safest workflow is therefore:

        Colab: scan -> pick a series -> write a .zip -> download it
        Laptop: drop the .zip on the dashboard

    `upload` is provided for the case where the instance is genuinely
    reachable from Colab - see the warning in that command.

USE IN A COLAB CELL
    !wget -q <raw-url-of-this-file> -O colab_export.py
    import colab_export as ce
    ce.scan('/content/manifest')                 # what series are here?
    ce.package('/content/manifest', series=0)    # write the first one as a zip
    ce.download('/content/exports/...zip')       # bring it to your laptop

Only DICOM headers are read while scanning, so a large collection is
inventoried quickly without decoding pixel data.
"""

import json
import os
import sys
import zipfile
from collections import defaultdict

try:
    import pydicom
except ImportError:  # pragma: no cover - Colab convenience
    print("pydicom is required:  !pip install pydicom")
    raise

DEFAULT_EXPORT_DIR = "/content/exports" if os.path.isdir("/content") else "./exports"
DICOM_SUFFIXES = (".dcm", ".dicom", "")


def _looks_like_dicom(path):
    """Cheap check: DICOM files carry 'DICM' at byte offset 128. Many public
    collections store files with no extension at all, so extension alone is
    not enough."""
    try:
        with open(path, "rb") as fh:
            fh.seek(128)
            return fh.read(4) == b"DICM"
    except OSError:
        return False


INVENTORY_FILE = "vitalitysync_inventory.json"


def scan(root, quiet=False, progress_every=2000, inventory=None, resume=True):
    """Groups every DICOM file under `root` by SeriesInstanceUID.

    Reads headers only (stop_before_pixels), so a large collection is
    inventoried without decoding pixel data. On a 24 GB tree this still means
    opening tens of thousands of files, so progress is reported and the
    result is cached: pass `inventory=<path>` to write it, and a later call
    with the same path reloads instead of rescanning.

    Returns a list of series dicts sorted by descending slice count.
    """
    inv_path = inventory or os.path.join(root, INVENTORY_FILE)
    if resume and os.path.exists(inv_path):
        try:
            with open(inv_path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if not quiet:
                print(f"Loaded cached inventory from {inv_path} "
                      f"({len(cached)} series). Pass resume=False to rescan.")
                summarize(cached)
            return cached
        except Exception:  # noqa: BLE001
            pass

    series = defaultdict(lambda: {"files": [], "meta": {}, "bytes": 0})
    scanned = skipped = seen = 0

    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn == INVENTORY_FILE:
                continue
            seen += 1
            if not quiet and progress_every and seen % progress_every == 0:
                print(f"  ...{seen:,} files examined, {len(series)} series so far", flush=True)

            path = os.path.join(dirpath, fn)
            if not (fn.lower().endswith(DICOM_SUFFIXES[:2]) or _looks_like_dicom(path)):
                skipped += 1
                continue
            try:
                ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
                uid = getattr(ds, "SeriesInstanceUID", None)
                if not uid:
                    skipped += 1
                    continue
            except Exception:  # noqa: BLE001
                skipped += 1
                continue

            scanned += 1
            entry = series[uid]
            entry["files"].append(path)
            try:
                entry["bytes"] += os.path.getsize(path)
            except OSError:
                pass
            if not entry["meta"]:
                entry["meta"] = {
                    "modality": str(getattr(ds, "Modality", "?")),
                    "body_part": str(getattr(ds, "BodyPartExamined", "?")),
                    "rows": int(getattr(ds, "Rows", 0) or 0),
                    "columns": int(getattr(ds, "Columns", 0) or 0),
                    "thickness": str(getattr(ds, "SliceThickness", "?")),
                    "manufacturer": str(getattr(ds, "Manufacturer", "?")),
                    "description": str(getattr(ds, "SeriesDescription", "")),
                }

    out = []
    for uid, entry in series.items():
        out.append({
            "series_uid": uid, "n_files": len(entry["files"]),
            "size_mb": round(entry["bytes"] / 1e6, 1),
            "files": sorted(entry["files"]), **entry["meta"],
        })
    out.sort(key=lambda s: -s["n_files"])

    try:
        with open(inv_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
        if not quiet:
            print(f"\nInventory cached at {inv_path}")
    except OSError:
        pass

    if not quiet:
        print(f"\nExamined {seen:,} files: {scanned:,} DICOM, {skipped:,} other.")
        summarize(out)
    return out


def summarize(series_list, top=20):
    """Prints a collection-level overview plus the largest series."""
    if not series_list:
        print("No DICOM series found.")
        return
    total_mb = sum(s.get("size_mb", 0) for s in series_list)
    usable = [s for s in series_list if s["n_files"] >= 40]
    print(f"\n{len(series_list)} series, {total_mb / 1000:.1f} GB total.")
    print(f"{len(usable)} series have >= 40 slices (usable for volume reconstruction).\n")
    print(f"  {'#':>4}  {'slices':>6}  {'MB':>7}  {'mod':<4} {'body':<8} {'matrix':<10} description")
    for i, s in enumerate(series_list[:top]):
        print(f"  {i:>4}  {s['n_files']:>6}  {s.get('size_mb', 0):>7.1f}  {s['modality']:<4} "
              f"{s['body_part'][:8]:<8} {s['rows']}x{s['columns']:<6} {s['description'][:30]}")
    if len(series_list) > top:
        print(f"  ... and {len(series_list) - top} more (raise `top=` to see them)")
    print("\nNext:  shortlist(inv)  then  package(root, series=<#>)")


def shortlist(series_list, modality="CT", body_part="CHEST", min_slices=150,
               max_slices=600, limit=10, require_hu=True):
    """Filters an inventory down to series worth importing.

    Defaults target what this application actually reconstructs well: a chest
    CT with enough slices for volumetric and zonal measurements, and small
    enough to stay within a sensible memory budget.
    """
    picks = []
    for s in series_list:
        if modality and s.get("modality", "").upper() != modality.upper():
            continue
        bp = (s.get("body_part") or "").upper()
        if body_part and body_part.upper() not in bp and bp not in ("", "?"):
            continue
        if not (min_slices <= s["n_files"] <= max_slices):
            continue
        picks.append(s)

    picks.sort(key=lambda s: -s["n_files"])
    picks = picks[:limit]

    print(f"{len(picks)} series match "
          f"(modality={modality}, body={body_part}, {min_slices}-{max_slices} slices):\n")
    for i, s in enumerate(picks):
        vol_mb = s["rows"] * s["columns"] * s["n_files"] * 4 / 1e6
        print(f"  [{i}] {s['n_files']:>4} slices  {s.get('size_mb', 0):>6.1f} MB on disk  "
              f"-> {vol_mb:>6.0f} MB volume, ~{vol_mb * 7 / 1000:.1f} GB peak RAM to analyse")
        print(f"       {s['series_uid']}")
    if picks:
        total = sum(s.get("size_mb", 0) for s in picks)
        print(f"\nDownloading all {len(picks)}: {total:.0f} MB "
              f"(instead of the whole collection).")
    return picks


def package(root, series=0, out_dir=None, series_list=None, max_mb=None):
    """Writes ONE series as a .zip ready for the dashboard.

    `series` is either an index into scan() results or a SeriesInstanceUID.
    Files are stored without compression by default because DICOM pixel data
    is already encoded - compressing again costs time for very little gain.
    """
    series_list = series_list or scan(root, quiet=True)
    if not series_list:
        print("No DICOM series found.")
        return None

    if isinstance(series, int):
        if series >= len(series_list):
            print(f"Only {len(series_list)} series found.")
            return None
        chosen = series_list[series]
    else:
        chosen = next((s for s in series_list if s["series_uid"] == series), None)
        if chosen is None:
            print(f"No series with UID {series}")
            return None

    out_dir = out_dir or DEFAULT_EXPORT_DIR
    os.makedirs(out_dir, exist_ok=True)
    name = f"series_{chosen['series_uid'][-16:]}_{chosen['n_files']}slices.zip"
    out_path = os.path.join(out_dir, name)

    total = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for i, path in enumerate(chosen["files"]):
            zf.write(path, arcname=f"{i:05d}.dcm")
            total += os.path.getsize(path)

    size_mb = os.path.getsize(out_path) / 1e6
    print(f"Wrote {out_path}")
    print(f"  {chosen['n_files']} slices, {size_mb:.1f} MB")
    if max_mb and size_mb > max_mb:
        print(f"  NOTE: larger than {max_mb} MB; browser downloads of very large files "
              f"from Colab are unreliable - consider Google Drive instead.")
    return out_path


def package_all(root, out_dir=None, min_slices=40, picks=None, max_total_gb=5.0):
    """Writes an archive for each selected series.

    `picks` is normally the output of shortlist(). Without it, every series
    with at least `min_slices` slices is packaged - which on a large
    collection is almost never what you want, so a total-size ceiling stops
    it before it fills the disk.
    """
    series_list = picks if picks is not None else [
        s for s in scan(root, quiet=True) if s["n_files"] >= min_slices]
    made, total_mb = [], 0.0
    for s in series_list:
        if total_mb / 1000 >= max_total_gb:
            print(f"\nStopped at the {max_total_gb} GB ceiling; "
                  f"{len(series_list) - len(made)} series not packaged. "
                  f"Raise max_total_gb if you really want more.")
            break
        path = package(root, series=s["series_uid"], out_dir=out_dir, series_list=series_list)
        if path:
            made.append(path)
            total_mb += os.path.getsize(path) / 1e6
    print(f"\nPackaged {len(made)} series, {total_mb:.0f} MB total.")
    return made


def download(path):
    """Downloads a file from Colab to your machine."""
    try:
        from google.colab import files
    except ImportError:
        print("Not running in Colab. Copy the file across yourself, for example:\n"
              f"  scp <host>:{path} .")
        return
    size_mb = os.path.getsize(path) / 1e6
    if size_mb > 200:
        print(f"WARNING: {size_mb:.0f} MB. Colab browser downloads often fail above "
              f"~200 MB - prefer to_drive() and download from Google Drive.")
    files.download(path)


def to_drive(path, folder="VitalitySync"):
    """Copies an archive to Google Drive, which is the reliable route for
    large files: mount Drive, copy, then download from drive.google.com."""
    try:
        from google.colab import drive
    except ImportError:
        print("Not running in Colab.")
        return None
    import shutil
    if not os.path.ismount("/content/drive"):
        drive.mount("/content/drive")
    dest_dir = f"/content/drive/MyDrive/{folder}"
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(path))
    shutil.copy2(path, dest)
    print(f"Copied to Google Drive: {dest}")
    print("Download it from drive.google.com, then drop it on the dashboard.")
    return dest


def upload(path, base_url, email, password, case_ref=None, analyze=False):
    """POSTs an archive to a reachable VitalitySync instance.

    ONLY works if `base_url` is reachable FROM COLAB. Your laptop's
    http://127.0.0.1:5050 is not - Colab would be calling its own loopback.
    Reaching a local instance means exposing it through a tunnel, which puts
    a medical application on the public internet; if you do that, keep the
    tunnel short-lived and use a strong account password. Uploading from
    Colab is otherwise unnecessary: the download route above avoids exposing
    anything.
    """
    # Checked before anything else: this is a mistake about what the URL means,
    # and it must be reported even in an environment where requests is absent.
    if "127.0.0.1" in base_url or "localhost" in base_url:
        print("REFUSING: 127.0.0.1/localhost from Colab points at Colab itself, not your\n"
              "laptop. Either download the archive and import it locally, or supply a\n"
              "URL that is genuinely reachable from here.")
        return None

    try:
        import requests
    except ImportError:
        print("!pip install requests")
        return None

    s = requests.Session()
    r = s.post(f"{base_url}/api/auth/login", json={"email": email, "password": password}, timeout=60)
    if r.status_code != 200:
        print(f"Sign-in failed ({r.status_code})")
        return None

    data = {"case_ref": case_ref} if case_ref else {}
    with open(path, "rb") as fh:
        r = s.post(f"{base_url}/api/dicom/upload",
                   files={"files": (os.path.basename(path), fh, "application/zip")},
                   data=data, timeout=3600)
    if r.status_code != 200:
        print(f"Upload failed ({r.status_code}): {r.text[:300]}")
        return None

    payload = r.json()
    study_id = payload["study_id"]
    summary = payload["summary"]
    for note in payload.get("archive_notes", []):
        print(f"  {note}")
    print(f"Imported study {study_id}: {summary['slice_count']} slices, "
          f"HU {summary['hu_conversion_status']}")

    if analyze:
        seg = s.post(f"{base_url}/api/dicom/study/{study_id}/segment-lungs", timeout=3600).json()
        if seg.get("success"):
            print(f"  lung volume {seg['stats'].get('lung_volume_ml')} mL")
        else:
            print(f"  segmentation did not pass: {seg.get('warnings')}")
    print(f"Open: {base_url}/viewer/{study_id}")
    return study_id


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    root = sys.argv[1]
    found = scan(root)
    if len(sys.argv) > 2 and found:
        package(root, series=int(sys.argv[2]), series_list=found)
