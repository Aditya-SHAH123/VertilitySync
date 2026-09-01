"""
Fetch a public, de-identified CT series and import it into a running
VitalitySync instance.

    # find candidate series in a public collection
    python tools/fetch_public_study.py search --collection LIDC-IDRI --min-slices 200

    # download one series to a local .zip
    python tools/fetch_public_study.py download <SeriesInstanceUID> -o study.zip

    # download and import in one step
    python tools/fetch_public_study.py import <SeriesInstanceUID> \
        --base http://127.0.0.1:5050 --email you@example.test

Data source
    The Cancer Imaging Archive (TCIA) public REST API. Only collections that
    TCIA serves without authentication are reachable this way.

Licensing is YOUR responsibility
    Each TCIA collection carries its own licence and data-use agreement. Many
    (including LIDC-IDRI) are Creative Commons Attribution, which permits reuse
    WITH ATTRIBUTION; others are more restrictive. This script prints a
    reminder but cannot check terms for you - read the collection page before
    using or redistributing the data.

Nothing downloaded by this script should be committed to the repository; the
default output paths are gitignored.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

TCIA_BASE = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
COLLECTION_PAGE = "https://www.cancerimagingarchive.net/collection/{}/"
TIMEOUT = 600


def _ssl_context():
    """Python installed from python.org on macOS ships without a usable root
    certificate store, so urllib fails TLS verification even though curl
    succeeds. Prefer certifi's bundle when it is available; never disable
    verification."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(path, params=None):
    url = f"{TCIA_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT, context=_ssl_context()) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            raise SystemExit(
                "TLS verification failed: this Python has no usable root certificate store.\n"
                "Install certifi into the environment you are running this with:\n"
                "    ./venv/bin/pip install certifi\n"
                "(On a python.org macOS build you can also run "
                "'Install Certificates.command' from the Python folder.)") from exc
        raise SystemExit(f"Could not reach TCIA: {exc}") from exc


def cmd_collections(_args):
    data = json.loads(_get("getCollectionValues"))
    names = sorted(d["Collection"] for d in data)
    print(f"{len(names)} public collections:\n")
    for n in names:
        print(f"  {n}")


def cmd_search(args):
    params = {"Collection": args.collection}
    if args.modality:
        params["Modality"] = args.modality
    series = json.loads(_get("getSeries", params))
    rows = []
    for s in series:
        try:
            count = int(s.get("ImageCount", 0))
        except (TypeError, ValueError):
            continue
        if count < args.min_slices or (args.max_slices and count > args.max_slices):
            continue
        rows.append((count, s))
    rows.sort(key=lambda r: r[0])

    print(f"{len(rows)} series in {args.collection} with "
          f"{args.min_slices}-{args.max_slices or 'inf'} images\n")
    for count, s in rows[: args.limit]:
        print(f"  {s['SeriesInstanceUID']}")
        print(f"    images {count:>4}   body part {s.get('BodyPartExamined', '-'):<12} "
              f"manufacturer {s.get('Manufacturer', '-')}")
    print(f"\nLicence and data-use terms: {COLLECTION_PAGE.format(args.collection.lower())}")


def cmd_download(args):
    out = args.output or f"{args.series_uid[:24]}.zip"
    print(f"Downloading series {args.series_uid}")
    data = _get("getImage", {"SeriesInstanceUID": args.series_uid})
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"  wrote {out}  ({len(data) / 1e6:.1f} MB)")
    print("\nImport it by dropping the .zip onto the dashboard, or run this "
          "script's `import` command.")
    return out


def cmd_import(args):
    """Downloads (unless given a local file) and POSTs the archive to a
    running instance, authenticating first."""
    try:
        import requests
    except ImportError:
        print("The `import` command needs the requests package:\n"
              "    ./venv/bin/pip install requests")
        sys.exit(1)

    path = args.file
    if not path:
        path = cmd_download(argparse.Namespace(series_uid=args.series_uid, output=args.output))

    if not os.path.exists(path):
        print(f"No such file: {path}")
        sys.exit(1)

    password = args.password or __import__("getpass").getpass(f"Password for {args.email}: ")

    s = requests.Session()
    r = s.post(f"{args.base}/api/auth/login",
               json={"email": args.email, "password": password}, timeout=60)
    if r.status_code != 200:
        print(f"Sign-in failed ({r.status_code}): {r.text[:200]}")
        sys.exit(1)
    print(f"Signed in to {args.base}")

    data = {}
    if args.case_ref:
        data["case_ref"] = args.case_ref

    print(f"Uploading {os.path.basename(path)} ({os.path.getsize(path) / 1e6:.1f} MB)…")
    with open(path, "rb") as fh:
        r = s.post(f"{args.base}/api/dicom/upload",
                   files={"files": (os.path.basename(path), fh, "application/zip")},
                   data=data, timeout=1800)
    payload = r.json()
    if r.status_code != 200:
        print(f"Upload failed ({r.status_code}): {payload.get('message')}")
        for note in payload.get("archive_notes", []):
            print(f"  {note}")
        sys.exit(1)

    for note in payload.get("archive_notes", []):
        print(f"  {note}")
    summary = payload["summary"]
    study_id = payload["study_id"]
    print(f"\nImported study {study_id}")
    print(f"  slices {summary['slice_count']}   "
          f"{summary['rows']}x{summary['columns']}   HU {summary['hu_conversion_status']}")

    if args.analyze:
        print("\nSegmenting…")
        r = s.post(f"{args.base}/api/dicom/study/{study_id}/segment-lungs", timeout=1800)
        seg = r.json()
        if r.status_code != 200 or not seg.get("success"):
            print(f"  segmentation did not pass: {seg.get('warnings')}")
        else:
            st = seg["stats"]
            print(f"  lung volume {st.get('lung_volume_ml')} mL   "
                  f"L/R {st.get('left_lung_volume_ml')}/{st.get('right_lung_volume_ml')} mL")
            print("\nAnalysing…")
            r = s.get(f"{args.base}/api/dicom/study/{study_id}/analysis", timeout=1800)
            if r.status_code == 200:
                a = r.json()["analysis"]
                w = a["density_metrics"]["whole_lungs"]
                print(f"  mean {w['mean_hu']} HU   median {w['median_hu']} HU   "
                      f"p5 {w['percentiles_hu']['p5']}   p95 {w['percentiles_hu']['p95']}")

    print(f"\nOpen it at:  {args.base}/viewer/{study_id}")
    print("\nReminder: check the collection's licence and data-use terms before "
          "reusing or redistributing this data.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("collections", help="list public TCIA collections").set_defaults(func=cmd_collections)

    p = sub.add_parser("search", help="find series in a collection")
    p.add_argument("--collection", default="LIDC-IDRI")
    p.add_argument("--modality", default="CT")
    p.add_argument("--min-slices", type=int, default=100)
    p.add_argument("--max-slices", type=int, default=0)
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("download", help="download one series as a .zip")
    p.add_argument("series_uid")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("import", help="download and import into a running instance")
    p.add_argument("series_uid", nargs="?")
    p.add_argument("--file", help="import an existing local .zip instead of downloading")
    p.add_argument("-o", "--output")
    p.add_argument("--base", default="http://127.0.0.1:5050")
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="prompted for if omitted")
    p.add_argument("--case-ref", help="attach the study to an existing case")
    p.add_argument("--analyze", action="store_true", help="also segment and analyse after import")
    p.set_defaults(func=cmd_import)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
