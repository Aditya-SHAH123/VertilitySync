"""
Durable, memory-bounded storage for reconstructed imaging studies.

Replaces the previous plain in-memory `STUDIES = {}` dict, which had two
problems: every study was lost on restart (and was invisible to other
serverless instances), and every study stayed resident in RAM forever - a
real 512x512x300 chest CT is ~314 MB as float32, so a handful of concurrent
studies could exhaust memory.

Design:
  * The HU volume is written to disk as a .npy file and memory-mapped on
    read, so slice extraction touches only the pages it needs rather than
    faulting the whole volume into RAM.
  * Non-array state (summary, geometry, ownership, segmentation stats) is
    stored as JSON alongside it.
  * Segmentation masks are stored bit-packed (np.packbits), which is 8x
    smaller than a bool array on disk.
  * A small LRU keeps the most recently used studies resident; everything
    else is evicted from memory but remains on disk.

The public surface is intentionally dict-like (`get`, `pop`, `in`,
`__setitem__`, `clear`) so existing call sites and tests continue to work
unchanged.

PRIVACY: study directories contain reconstructed pixel data. They live under
STUDY_STORE_PATH, which is gitignored, and are deleted by `pop()`. Only the
non-identifying technical summary already produced by the DICOM pipeline is
serialized - no patient-identifying DICOM tags are ever written here.
"""

import json
import os
import shutil
import threading
from collections import OrderedDict

import numpy as np

from mesh_reconstruction import VolumeGeometry

DEFAULT_STORE_PATH = os.environ.get(
    "STUDY_STORE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance", "studies"),
)

# How many studies stay resident in memory. Kept small on purpose: a real
# chest CT volume is hundreds of megabytes.
DEFAULT_CACHE_SIZE = int(os.environ.get("STUDY_CACHE_SIZE", "3"))

VOLUME_FILE = "volume.npy"
META_FILE = "meta.json"
SEG_FILE = "segmentation.npz"
ANALYSIS_FILE = "analysis.json"


def _geometry_to_json(geometry):
    if geometry is None:
        return None
    return geometry.to_public_dict()


def _geometry_from_json(data):
    if not data:
        return None
    shape = data["shape"]
    return VolumeGeometry(
        origin_mm=tuple(data["origin_mm"]),
        spacing_mm=tuple(data["spacing_mm"]),
        col_cosines=tuple(data["col_cosines"]),
        row_cosines=tuple(data["row_cosines"]),
        slice_cosines=tuple(data["slice_cosines"]),
        orientation_reliable=data["orientation_reliable"],
        shape=(shape["slices"], shape["rows"], shape["cols"]),
    )


class _StoredSegmentation:
    """Rehydrated segmentation result.

    Mirrors the attribute surface of lung_segmentation.SegmentationResult that
    the routes actually use (`success`, `status`, `method`, `warnings`,
    `stats`, `mask`, `left_mask`, `right_mask`), so reconstruct3d works
    identically whether the segmentation was just computed or loaded from disk.
    """

    def __init__(self, payload, masks):
        self.success = payload["success"]
        self.status = payload["status"]
        self.method = payload["method"]
        self.method_version = payload["method_version"]
        self.warnings = payload["warnings"]
        self.stats = payload["stats"]
        self.mask = masks.get("mask")
        self.left_mask = masks.get("left_mask")
        self.right_mask = masks.get("right_mask")

    def to_public_dict(self):
        return {
            "success": self.success, "status": self.status, "method": self.method,
            "method_version": self.method_version, "warnings": self.warnings,
            "stats": self.stats,
            "left_right_available": self.left_mask is not None and self.right_mask is not None,
        }


class StudyStore:
    def __init__(self, path=None, cache_size=None):
        self.path = path or DEFAULT_STORE_PATH
        self.cache_size = cache_size or DEFAULT_CACHE_SIZE
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        os.makedirs(self.path, exist_ok=True)

    # ---------------- paths ----------------

    def _dir(self, study_id):
        # study ids are server-generated uuid4s; reject anything that could
        # escape the store directory.
        if not study_id or "/" in study_id or "\\" in study_id or ".." in study_id:
            raise ValueError(f"Unsafe study id: {study_id!r}")
        return os.path.join(self.path, study_id)

    # ---------------- write ----------------

    def __setitem__(self, study_id, study):
        self.put(study_id, study)

    def put(self, study_id, study):
        d = self._dir(study_id)
        os.makedirs(d, exist_ok=True)

        volume = study.get("hu_volume")
        if volume is not None:
            np.save(os.path.join(d, VOLUME_FILE), np.ascontiguousarray(volume, dtype=np.float32))

        meta = {
            "hu_available_per_slice": [bool(v) for v in (study.get("hu_available_per_slice") or [])],
            "pixel_spacing": study.get("pixel_spacing"),
            "slice_spacing": study.get("slice_spacing"),
            "summary": study.get("summary"),
            "geometry": _geometry_to_json(study.get("geometry")),
            "owner_doctor_id": study.get("owner_doctor_id"),
            "created_at": study.get("created_at"),
            "has_volume": volume is not None,
        }
        with open(os.path.join(d, META_FILE), "w", encoding="utf-8") as fh:
            json.dump(meta, fh)

        self._write_segmentation(d, study.get("segmentation"))

        analysis = study.get("analysis")
        analysis_path = os.path.join(d, ANALYSIS_FILE)
        if analysis is not None:
            with open(analysis_path, "w", encoding="utf-8") as fh:
                json.dump(analysis, fh)
        elif os.path.exists(analysis_path):
            os.remove(analysis_path)

        with self._lock:
            self._cache[study_id] = study
            self._cache.move_to_end(study_id)
            self._evict()

    def _write_segmentation(self, d, segmentation):
        seg_path = os.path.join(d, SEG_FILE)
        if segmentation is None:
            if os.path.exists(seg_path):
                os.remove(seg_path)
            return
        arrays = {}
        for name in ("mask", "left_mask", "right_mask"):
            m = getattr(segmentation, name, None)
            if m is not None:
                # bit-pack booleans: 8x smaller on disk than a bool array
                arrays[name] = np.packbits(np.ascontiguousarray(m, dtype=bool))
                arrays[name + "__shape"] = np.array(m.shape, dtype=np.int64)
        arrays["__payload"] = np.frombuffer(
            json.dumps({
                "success": segmentation.success, "status": segmentation.status,
                "method": segmentation.method, "method_version": segmentation.method_version,
                "warnings": list(segmentation.warnings), "stats": dict(segmentation.stats),
            }).encode("utf-8"), dtype=np.uint8)
        np.savez_compressed(seg_path, **arrays)

    def save_analysis(self, study_id, analysis):
        """Persists the quantitative analysis so it survives a restart and an
        eviction from the memory cache. It is derived purely from the volume
        and the segmentation, so recomputing it is wasted work."""
        d = self._dir(study_id)
        if not os.path.isdir(d):
            raise KeyError(study_id)
        with open(os.path.join(d, ANALYSIS_FILE), "w", encoding="utf-8") as fh:
            json.dump(analysis, fh)
        with self._lock:
            if study_id in self._cache:
                self._cache[study_id]["analysis"] = analysis

    def _load_analysis(self, d):
        p = os.path.join(d, ANALYSIS_FILE)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def save_segmentation(self, study_id, segmentation):
        """Persists a segmentation computed after the study was first stored."""
        d = self._dir(study_id)
        if not os.path.isdir(d):
            raise KeyError(study_id)
        self._write_segmentation(d, segmentation)
        with self._lock:
            if study_id in self._cache:
                self._cache[study_id]["segmentation"] = segmentation

    # ---------------- read ----------------

    def get(self, study_id, default=None):
        try:
            with self._lock:
                if study_id in self._cache:
                    self._cache.move_to_end(study_id)
                    return self._cache[study_id]
            study = self._load(study_id)
        except (ValueError, KeyError):
            return default
        if study is None:
            return default
        with self._lock:
            self._cache[study_id] = study
            self._cache.move_to_end(study_id)
            self._evict()
        return study

    def _load(self, study_id):
        d = self._dir(study_id)
        meta_path = os.path.join(d, META_FILE)
        if not os.path.exists(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)

        volume = None
        vol_path = os.path.join(d, VOLUME_FILE)
        if meta.get("has_volume") and os.path.exists(vol_path):
            # mmap: slice reads fault in only the pages they touch, so opening
            # a large study does not pull the whole volume into RAM.
            volume = np.load(vol_path, mmap_mode="r")

        study = {
            "hu_volume": volume,
            "hu_available_per_slice": meta.get("hu_available_per_slice") or [],
            "pixel_spacing": meta.get("pixel_spacing"),
            "slice_spacing": meta.get("slice_spacing"),
            "summary": meta.get("summary"),
            "geometry": _geometry_from_json(meta.get("geometry")),
            "owner_doctor_id": meta.get("owner_doctor_id"),
            "created_at": meta.get("created_at"),
            "segmentation": self._load_segmentation(d),
            "analysis": self._load_analysis(d),
        }
        return study

    def _load_segmentation(self, d):
        seg_path = os.path.join(d, SEG_FILE)
        if not os.path.exists(seg_path):
            return None
        with np.load(seg_path) as z:
            payload = json.loads(bytes(z["__payload"]).decode("utf-8"))
            masks = {}
            for name in ("mask", "left_mask", "right_mask"):
                if name in z:
                    shape = tuple(int(v) for v in z[name + "__shape"])
                    count = int(np.prod(shape))
                    masks[name] = np.unpackbits(z[name], count=count).astype(bool).reshape(shape)
        return _StoredSegmentation(payload, masks)

    # ---------------- delete / membership ----------------

    def pop(self, study_id, default=None):
        try:
            d = self._dir(study_id)
        except ValueError:
            return default
        with self._lock:
            existed = self._cache.pop(study_id, None)
        on_disk = os.path.isdir(d)
        if on_disk:
            shutil.rmtree(d, ignore_errors=True)
        if existed is None and not on_disk:
            return default
        return existed if existed is not None else True

    def __contains__(self, study_id):
        with self._lock:
            if study_id in self._cache:
                return True
        try:
            return os.path.exists(os.path.join(self._dir(study_id), META_FILE))
        except ValueError:
            return False

    def clear(self):
        with self._lock:
            self._cache.clear()
        if os.path.isdir(self.path):
            shutil.rmtree(self.path, ignore_errors=True)
        os.makedirs(self.path, exist_ok=True)

    def keys(self):
        if not os.path.isdir(self.path):
            return []
        return [name for name in os.listdir(self.path)
                if os.path.exists(os.path.join(self.path, name, META_FILE))]

    def __len__(self):
        return len(self.keys())

    def _evict(self):
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    @property
    def resident_count(self):
        with self._lock:
            return len(self._cache)
