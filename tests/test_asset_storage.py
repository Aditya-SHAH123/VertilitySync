"""
Tests for api/asset_storage.py.

The S3 backend is tested against moto's mocked AWS (test-only dependency,
see tests/requirements-test.txt) - no real bucket, no network call, no
credentials needed or used.
"""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))

from asset_storage import (  # noqa: E402
    LocalDiskAssetStore, S3AssetStore, AssetNotFoundError, get_asset_store,
)

try:
    from moto import mock_aws
    HAVE_MOTO = True
except ImportError:
    HAVE_MOTO = False


class TestLocalDiskAssetStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.store = LocalDiskAssetStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_put_then_get_round_trip(self):
        self.store.put_bytes("studies/abc/volume.bin", b"\x00\x01\x02fake-volume-bytes")
        self.assertEqual(self.store.get_bytes("studies/abc/volume.bin"),
                          b"\x00\x01\x02fake-volume-bytes")

    def test_exists(self):
        self.assertFalse(self.store.exists("nope"))
        self.store.put_bytes("nope", b"now it does")
        self.assertTrue(self.store.exists("nope"))

    def test_get_missing_raises(self):
        with self.assertRaises(AssetNotFoundError):
            self.store.get_bytes("never-written")

    def test_delete_is_idempotent(self):
        self.store.put_bytes("x", b"data")
        self.store.delete("x")
        self.assertFalse(self.store.exists("x"))
        self.store.delete("x")  # second delete must not raise

    def test_nested_key_creates_subdirectories(self):
        self.store.put_bytes("a/b/c/d.bin", b"deep")
        self.assertEqual(self.store.get_bytes("a/b/c/d.bin"), b"deep")

    def test_overwrite_replaces_content(self):
        self.store.put_bytes("k", b"first")
        self.store.put_bytes("k", b"second")
        self.assertEqual(self.store.get_bytes("k"), b"second")

    def test_path_traversal_key_rejected(self):
        with self.assertRaises(ValueError):
            self.store.put_bytes("../../etc/passwd", b"malicious")

    def test_absolute_key_rejected(self):
        with self.assertRaises(ValueError):
            self.store.put_bytes("/etc/passwd", b"malicious")

    def test_default_root_is_not_a_flask_static_folder(self):
        from asset_storage import DEFAULT_LOCAL_ASSET_PATH
        self.assertNotIn("static", DEFAULT_LOCAL_ASSET_PATH)
        self.assertNotIn("templates", DEFAULT_LOCAL_ASSET_PATH)


@unittest.skipUnless(HAVE_MOTO, "moto not installed")
class TestS3AssetStore(unittest.TestCase):
    def setUp(self):
        self._mock = mock_aws()
        self._mock.start()
        import boto3
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-imaging-bucket")
        self.store = S3AssetStore(bucket="test-imaging-bucket", prefix="assets",
                                   region_name="us-east-1")

    def tearDown(self):
        self._mock.stop()

    def test_put_then_get_round_trip(self):
        self.store.put_bytes("studies/xyz/mask.npz", b"packed-mask-bytes")
        self.assertEqual(self.store.get_bytes("studies/xyz/mask.npz"), b"packed-mask-bytes")

    def test_get_missing_raises_asset_not_found(self):
        with self.assertRaises(AssetNotFoundError):
            self.store.get_bytes("never-written")

    def test_exists(self):
        self.assertFalse(self.store.exists("k"))
        self.store.put_bytes("k", b"v")
        self.assertTrue(self.store.exists("k"))

    def test_delete(self):
        self.store.put_bytes("k", b"v")
        self.store.delete("k")
        self.assertFalse(self.store.exists("k"))

    def test_prefix_isolates_keys_from_bucket_root(self):
        import boto3
        self.store.put_bytes("k", b"v")
        client = boto3.client("s3", region_name="us-east-1")
        listing = client.list_objects_v2(Bucket="test-imaging-bucket")
        keys = [obj["Key"] for obj in listing.get("Contents", [])]
        self.assertEqual(keys, ["assets/k"])

    def test_missing_bucket_raises_value_error(self):
        with self.assertRaises(ValueError):
            S3AssetStore(bucket=None)


class TestGetAssetStoreFactory(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                        ("ASSET_STORAGE_BACKEND", "ASSET_STORAGE_LOCAL_PATH",
                         "S3_BUCKET", "S3_ENDPOINT_URL", "S3_REGION", "S3_PREFIX")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults_to_local(self):
        os.environ.pop("ASSET_STORAGE_BACKEND", None)
        store = get_asset_store()
        self.assertIsInstance(store, LocalDiskAssetStore)

    def test_explicit_local(self):
        os.environ["ASSET_STORAGE_BACKEND"] = "local"
        store = get_asset_store()
        self.assertIsInstance(store, LocalDiskAssetStore)

    @unittest.skipUnless(HAVE_MOTO, "moto not installed")
    def test_explicit_s3(self):
        os.environ["ASSET_STORAGE_BACKEND"] = "s3"
        os.environ["S3_BUCKET"] = "whatever-bucket"
        with mock_aws():
            store = get_asset_store()
            self.assertIsInstance(store, S3AssetStore)

    def test_unknown_backend_rejected(self):
        os.environ["ASSET_STORAGE_BACKEND"] = "dropbox"
        with self.assertRaises(ValueError):
            get_asset_store()


if __name__ == "__main__":
    import sys
    result = unittest.main(exit=False).result
    print(f'\n{result.testsRun - len(result.failures) - len(result.errors)} passed, '
          f'{len(result.failures) + len(result.errors)} failed')
    sys.exit(0 if result.wasSuccessful() else 1)
