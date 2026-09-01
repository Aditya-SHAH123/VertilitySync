"""
CRUD helpers for the imaging-relational layer (api/models.py).

Contains no authorization logic, matching cases.py's convention: every
route in api/index.py that calls into this module must have already
resolved the study through `_get_owned_study_or_error`, which is the single
place study ownership is checked (see auth.py's equivalent doctrine for
cases). A function here trusts the study_id it's given.

All geometry is patient-space millimetres (never screen pixels - see master
spec section 31); `geometry_json` / `position_json` are JSON-encoded lists
of [x_mm, y_mm, z_mm] points.
"""

import json

from db_engine import get_session
from models import ImagingStudy, ImagingSeries, ProcessingJob, RegionOfInterest, Measurement, Annotation


def _dt(value):
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
# ImagingStudy / ImagingSeries
# ---------------------------------------------------------------------------

def upsert_imaging_study(study_id, owner_doctor_id, case_id=None, modality=None,
                          slice_count=None, rows=None, columns=None,
                          asset_hash=None, storage_backend="local", storage_key=None):
    with get_session() as s:
        study = s.get(ImagingStudy, study_id)
        if study is None:
            study = ImagingStudy(id=study_id, owner_doctor_id=owner_doctor_id)
            s.add(study)
        study.case_id = case_id
        study.modality = modality
        study.slice_count = slice_count
        study.rows = rows
        study.columns = columns
        study.asset_hash = asset_hash
        study.storage_backend = storage_backend
        study.storage_key = storage_key or study_id
        s.flush()
        return study.id


def update_study_status(study_id, status):
    with get_session() as s:
        study = s.get(ImagingStudy, study_id)
        if study is None:
            raise KeyError(study_id)
        study.status = status


def add_series(study_id, series_instance_uid=None, slice_count=None, rows=None,
                columns=None, pixel_spacing_row_mm=None, pixel_spacing_col_mm=None,
                slice_spacing_mm=None, orientation_reliable=False,
                reconstruction_kernel=None, hu_available=False,
                fallback_ordering_used=False):
    with get_session() as s:
        series = ImagingSeries(
            study_id=study_id, series_instance_uid=series_instance_uid,
            slice_count=slice_count, rows=rows, columns=columns,
            pixel_spacing_row_mm=pixel_spacing_row_mm,
            pixel_spacing_col_mm=pixel_spacing_col_mm,
            slice_spacing_mm=slice_spacing_mm,
            orientation_reliable=orientation_reliable,
            reconstruction_kernel=reconstruction_kernel,
            hu_available=hu_available,
            fallback_ordering_used=fallback_ordering_used,
        )
        s.add(series)
        s.flush()
        return series.id


# ---------------------------------------------------------------------------
# Processing jobs
# ---------------------------------------------------------------------------

def start_job(study_id, job_type, method=None, method_version=None, parameters=None):
    if job_type not in ProcessingJob.JOB_TYPES:
        raise ValueError(f"job_type must be one of {ProcessingJob.JOB_TYPES}")
    with get_session() as s:
        job = ProcessingJob(
            study_id=study_id, job_type=job_type, status="RUNNING",
            method=method, method_version=method_version,
            parameters_json=json.dumps(parameters) if parameters is not None else None,
        )
        from models import _now
        job.started_at = _now()
        s.add(job)
        s.flush()
        return job.id


def complete_job(job_id, progress=1.0):
    from models import _now
    with get_session() as s:
        job = s.get(ProcessingJob, job_id)
        if job is None:
            raise KeyError(job_id)
        job.status = "COMPLETED"
        job.progress = progress
        job.completed_at = _now()


def fail_job(job_id, error_message):
    from models import _now
    with get_session() as s:
        job = s.get(ProcessingJob, job_id)
        if job is None:
            raise KeyError(job_id)
        job.status = "FAILED"
        job.error_message = str(error_message)
        job.completed_at = _now()


def list_jobs(study_id):
    with get_session() as s:
        rows = (s.query(ProcessingJob)
                 .filter_by(study_id=study_id)
                 .order_by(ProcessingJob.created_at.desc()).all())
        return [{
            "id": j.id, "job_type": j.job_type, "status": j.status,
            "progress": j.progress, "method": j.method, "method_version": j.method_version,
            "error_message": j.error_message,
            "created_at": _dt(j.created_at), "started_at": _dt(j.started_at),
            "completed_at": _dt(j.completed_at),
        } for j in rows]


# ---------------------------------------------------------------------------
# Regions of interest
# ---------------------------------------------------------------------------

def create_region(study_id, source, centroid_mm=None, bbox=None, volume_ml=None,
                   mean_hu=None, median_hu=None, laterality=None, zone=None,
                   label=None, created_by_doctor_id=None, provenance=None):
    if source not in RegionOfInterest.SOURCES:
        raise ValueError(f"source must be one of {RegionOfInterest.SOURCES}")
    if source == "deterministic_segmentation" and label is not None:
        raise ValueError("A deterministic region cannot carry a clinician label.")
    with get_session() as s:
        r = RegionOfInterest(
            study_id=study_id, source=source, label=label,
            centroid_x_mm=centroid_mm[0] if centroid_mm else None,
            centroid_y_mm=centroid_mm[1] if centroid_mm else None,
            centroid_z_mm=centroid_mm[2] if centroid_mm else None,
            bbox_json=json.dumps(bbox) if bbox is not None else None,
            volume_ml=volume_ml, mean_hu=mean_hu, median_hu=median_hu,
            laterality=laterality, zone=zone,
            created_by_doctor_id=created_by_doctor_id,
            provenance_json=json.dumps(provenance) if provenance is not None else None,
        )
        s.add(r)
        s.flush()
        return r.id


def _region_to_dict(r):
    return {
        "id": r.id, "study_id": r.study_id, "source": r.source, "label": r.label,
        "centroid_mm": [r.centroid_x_mm, r.centroid_y_mm, r.centroid_z_mm]
                        if r.centroid_x_mm is not None else None,
        "bbox": json.loads(r.bbox_json) if r.bbox_json else None,
        "volume_ml": r.volume_ml, "mean_hu": r.mean_hu, "median_hu": r.median_hu,
        "laterality": r.laterality, "zone": r.zone,
        "created_by_doctor_id": r.created_by_doctor_id,
        "created_at": _dt(r.created_at),
        "provenance": json.loads(r.provenance_json) if r.provenance_json else None,
    }


def list_regions(study_id, source=None):
    with get_session() as s:
        q = s.query(RegionOfInterest).filter_by(study_id=study_id)
        if source is not None:
            q = q.filter_by(source=source)
        return [_region_to_dict(r) for r in q.order_by(RegionOfInterest.created_at.desc()).all()]


def delete_region(region_id, created_by_doctor_id):
    """Only the clinician who created a region may delete it; deterministic
    regions (created_by_doctor_id is null) cannot be deleted through this
    path - they are re-derived by re-running the analysis, not edited."""
    with get_session() as s:
        r = s.get(RegionOfInterest, region_id)
        if r is None:
            raise KeyError(region_id)
        if r.created_by_doctor_id != created_by_doctor_id:
            raise PermissionError("Only the creating doctor may delete this region.")
        s.delete(r)


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------

def create_measurement(study_id, measurement_type, geometry_mm, value, units,
                        created_by_doctor_id, mean_hu=None, median_hu=None,
                        hu_stddev=None, provenance=None):
    if measurement_type not in Measurement.TYPES:
        raise ValueError(f"measurement_type must be one of {Measurement.TYPES}")
    if not geometry_mm:
        raise ValueError("geometry_mm must contain at least one point.")
    with get_session() as s:
        m = Measurement(
            study_id=study_id, measurement_type=measurement_type,
            geometry_json=json.dumps(geometry_mm), value=value, units=units,
            mean_hu=mean_hu, median_hu=median_hu, hu_stddev=hu_stddev,
            created_by_doctor_id=created_by_doctor_id,
            provenance_json=json.dumps(provenance) if provenance is not None else None,
        )
        s.add(m)
        s.flush()
        return m.id


def _measurement_to_dict(m):
    return {
        "id": m.id, "study_id": m.study_id, "measurement_type": m.measurement_type,
        "geometry_mm": json.loads(m.geometry_json), "value": m.value, "units": m.units,
        "mean_hu": m.mean_hu, "median_hu": m.median_hu, "hu_stddev": m.hu_stddev,
        "created_by_doctor_id": m.created_by_doctor_id,
        "created_at": _dt(m.created_at), "updated_at": _dt(m.updated_at),
        "provenance": json.loads(m.provenance_json) if m.provenance_json else None,
    }


def list_measurements(study_id):
    with get_session() as s:
        rows = (s.query(Measurement).filter_by(study_id=study_id)
                 .order_by(Measurement.created_at.desc()).all())
        return [_measurement_to_dict(m) for m in rows]


def delete_measurement(measurement_id, created_by_doctor_id):
    with get_session() as s:
        m = s.get(Measurement, measurement_id)
        if m is None:
            raise KeyError(measurement_id)
        if m.created_by_doctor_id != created_by_doctor_id:
            raise PermissionError("Only the creating doctor may delete this measurement.")
        s.delete(m)


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

def create_annotation(study_id, text, created_by_doctor_id, position_mm=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("Annotation text cannot be empty.")
    with get_session() as s:
        a = Annotation(
            study_id=study_id, text=text, created_by_doctor_id=created_by_doctor_id,
            position_json=json.dumps(position_mm) if position_mm is not None else None,
        )
        s.add(a)
        s.flush()
        return a.id


def _annotation_to_dict(a):
    return {
        "id": a.id, "study_id": a.study_id, "text": a.text,
        "position_mm": json.loads(a.position_json) if a.position_json else None,
        "created_by_doctor_id": a.created_by_doctor_id,
        "created_at": _dt(a.created_at), "updated_at": _dt(a.updated_at),
    }


def list_annotations(study_id):
    with get_session() as s:
        rows = (s.query(Annotation).filter_by(study_id=study_id)
                 .order_by(Annotation.created_at.desc()).all())
        return [_annotation_to_dict(a) for a in rows]


def delete_annotation(annotation_id, created_by_doctor_id):
    with get_session() as s:
        a = s.get(Annotation, annotation_id)
        if a is None:
            raise KeyError(annotation_id)
        if a.created_by_doctor_id != created_by_doctor_id:
            raise PermissionError("Only the creating doctor may delete this annotation.")
        s.delete(a)
