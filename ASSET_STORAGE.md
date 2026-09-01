# Private Medical Asset Storage

`api/asset_storage.py` gives every caller one interface —
`put_bytes`/`get_bytes`/`delete`/`exists`, addressed by an opaque string
key — so large binary imaging assets never need to live in a relational
column or a publicly served static directory, and so the backing store can
change (local disk in dev, an S3-compatible bucket in production) without
call sites caring which.

## Backends

**`LocalDiskAssetStore`** (default). Writes under a private directory
(`instance/assets/` by default — override with `ASSET_STORAGE_LOCAL_PATH`),
never under a Flask `static/` or `templates/` folder. Rejects any key
containing `..` or a leading `/`, so a malicious key cannot escape the
store root.

**`S3AssetStore`**. Any S3-compatible endpoint — AWS S3, Cloudflare R2,
MinIO. Configure via:

```
ASSET_STORAGE_BACKEND=s3
S3_BUCKET=your-bucket
S3_PREFIX=optional/key/prefix
S3_ENDPOINT_URL=https://...   # set this for R2/MinIO; omit for AWS S3
S3_REGION=us-east-1
```

Credentials are read through boto3's normal credential chain (environment
variables, a shared config file, or an IAM role) — this module never reads,
logs, or hardcodes a key or secret. `boto3` is imported lazily inside
`S3AssetStore.__init__`, so a deployment that only ever uses the local
backend does not need it installed (it lives in `requirements-dev.txt`, not
the deployed function's `requirements.txt`).

## Selecting a backend

```python
from asset_storage import get_asset_store
store = get_asset_store()   # reads ASSET_STORAGE_BACKEND from the environment
```

Called fresh (not memoized) on each use, so switching backends between
requests or test cases doesn't require restarting anything.

## What this does NOT do yet

`study_store.py` — the existing durable storage for reconstructed HU
volumes, segmentation masks, and analysis JSON — does **not** go through
this abstraction. It still writes `.npy`/`.npz`/`.json` files directly
under `STUDY_STORE_PATH`. Rewiring it is a real migration: the five
studies currently on disk under `instance/studies/` are accumulated,
verified-against-real-data results, not disposable fixtures (see
`ARCHITECTURE_AUDIT.md` section 6), and `study_store.py`'s dict-like API
(`get`/`pop`/`in`/`__setitem__`) is depended on by every existing route and
test. That migration is future work, not attempted in this change.

This module exists now so the *new* relational entities that need to store
bytes have a real, tested backend from day one — and so the `study_store.py`
migration has somewhere real to land later.

## What was verified

19 tests in `tests/test_asset_storage.py`:

- `LocalDiskAssetStore`: round-trip, overwrite, idempotent delete, nested
  keys, path-traversal and absolute-path key rejection, and a check that
  the default root is not under `static/` or `templates/`.
- `S3AssetStore`, against **moto's mocked AWS** (no real bucket, no network
  call, no credentials needed or used): round-trip, missing-key raises
  `AssetNotFoundError` (not a raw boto3 exception, so callers don't need to
  know which backend is active), prefix isolation verified by listing the
  actual mocked bucket contents and checking the stored key, and rejection
  of a missing bucket name.
- The backend factory (`get_asset_store`): defaults to local, honours an
  explicit backend choice, and rejects an unknown backend name.
