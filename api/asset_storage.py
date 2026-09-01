"""
Private medical-asset storage abstraction.

WHAT THIS IS FOR
    Large binary imaging assets (reconstructed HU volumes, segmentation
    masks, meshes, exports) must never live in a relational database column
    or a publicly served static directory (master spec section 3). This
    module gives every caller one interface - put/get/delete/exists,
    addressed by an opaque key - so the actual backing store can be a local
    directory in development and an S3-compatible bucket (AWS S3, or an
    R2/MinIO endpoint) in production, without call sites caring which.

WHAT THIS IS NOT (YET)
    study_store.py does not go through this abstraction today - it still
    writes .npy/.npz/.json files directly under STUDY_STORE_PATH. Rewiring
    it is a real migration (see ARCHITECTURE_AUDIT.md section 6: existing
    study directories are real accumulated data, not disposable) and hasn't
    been done in this change. This module exists now so that the *new*
    relational entities that need to store bytes - measurement snapshots,
    exports, and eventually re-pointed study assets - have a real backend
    from day one, and so the study_store.py migration has something to move
    onto later.

BACKEND SELECTION (env)
    ASSET_STORAGE_BACKEND = "local" (default) or "s3"
    ASSET_STORAGE_LOCAL_PATH - local backend root (default instance/assets)
    S3_BUCKET - required for the s3 backend
    S3_ENDPOINT_URL - optional; set this for R2/MinIO/any S3-compatible host
    S3_REGION - optional
    AWS credentials are read via boto3's normal credential chain
    (environment, shared config file, or an IAM role) - never hardcoded and
    never logged by this module.
"""

import io
import os
from abc import ABC, abstractmethod

_INSTANCE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance"
)
DEFAULT_LOCAL_ASSET_PATH = os.path.join(_INSTANCE_DIR, "assets")


class AssetNotFoundError(KeyError):
    pass


def _validate_key(key):
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError(f"Unsafe asset key: {key!r}")


class AssetStore(ABC):
    @abstractmethod
    def put_bytes(self, key: str, data: bytes) -> str:
        """Writes data under key. Returns the key (or backend URI) for
        storage as a `storage_key` reference in the relational layer."""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """Raises AssetNotFoundError if the key does not exist."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """No-op if the key does not exist - deletion is idempotent."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...


class LocalDiskAssetStore(AssetStore):
    """Development/default backend: a private directory, never under a
    Flask static folder or otherwise publicly served."""

    def __init__(self, root=None):
        self.root = root or DEFAULT_LOCAL_ASSET_PATH
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key):
        _validate_key(key)
        return os.path.join(self.root, key)

    def put_bytes(self, key, data):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return key

    def get_bytes(self, key):
        path = self._path(key)
        if not os.path.exists(path):
            raise AssetNotFoundError(key)
        with open(path, "rb") as fh:
            return fh.read()

    def delete(self, key):
        path = self._path(key)
        if os.path.exists(path):
            os.remove(path)

    def exists(self, key):
        return os.path.exists(self._path(key))


class S3AssetStore(AssetStore):
    """S3-compatible backend (AWS S3, Cloudflare R2, MinIO, ...).

    Imports boto3 lazily so a deployment that only ever uses the local
    backend does not require it to be installed.
    """

    def __init__(self, bucket, prefix="", endpoint_url=None, region_name=None):
        import boto3  # local import: see class docstring
        if not bucket:
            raise ValueError("S3AssetStore requires a bucket name.")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url, region_name=region_name,
        )

    def _object_key(self, key):
        _validate_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_bytes(self, key, data):
        from botocore.exceptions import ClientError  # noqa: F401 (documents dependency)
        self._client.put_object(Bucket=self.bucket, Key=self._object_key(key), Body=data)
        return key

    def get_bytes(self, key):
        from botocore.exceptions import ClientError
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._object_key(key))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise AssetNotFoundError(key) from exc
            raise
        return resp["Body"].read()

    def delete(self, key):
        self._client.delete_object(Bucket=self.bucket, Key=self._object_key(key))

    def exists(self, key):
        from botocore.exceptions import ClientError
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._object_key(key))
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey"):
                return False
            raise


def get_asset_store():
    """Factory reading backend selection from the environment. Called fresh
    (not memoized) so tests can flip ASSET_STORAGE_BACKEND between cases."""
    backend = os.environ.get("ASSET_STORAGE_BACKEND", "local").strip().lower()
    if backend == "local":
        return LocalDiskAssetStore(os.environ.get("ASSET_STORAGE_LOCAL_PATH"))
    if backend == "s3":
        bucket = os.environ.get("S3_BUCKET")
        return S3AssetStore(
            bucket=bucket,
            prefix=os.environ.get("S3_PREFIX", ""),
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            region_name=os.environ.get("S3_REGION"),
        )
    raise ValueError(f"Unknown ASSET_STORAGE_BACKEND: {backend!r}")
