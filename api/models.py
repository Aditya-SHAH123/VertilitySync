"""
SQLAlchemy models for the imaging-relational layer.

WHY THIS IS SEPARATE FROM db.py
    db.py (doctors, cases, case_access, notes, audit_log) is a working,
    tested, stdlib-sqlite3 module. Rewriting it to an ORM is not required to
    add the capabilities this module exists for - relational tracking of
    imaging studies/series, background-job bookkeeping, and persisted
    clinician measurements/annotations/regions-of-interest - so it hasn't
    been touched. See ARCHITECTURE_AUDIT.md section 7.

    These tables are the ones that actually need an ORM: Postgres is the
    eventual target (see ARCHITECTURE_AUDIT.md section 4), and hand-writing
    parallel SQLite/Postgres dialect SQL for every query here is exactly the
    kind of duplication SQLAlchemy + Alembic exist to remove.

RELATIONSHIP TO cases.py / db.py
    ImagingStudy.case_id is a plain indexed integer, not a declared foreign
    key. It references db.py's `cases.id`, but that table is not modeled
    here (different engine/session, and today, for local SQLite dev, a
    different physical file - see db_engine.py). The reference is enforced
    at the application layer, the same way cases.py already references
    imaging by an opaque `study_id` string with no DB-level FK back into the
    file-based study store. When both layers eventually share one Postgres
    database, a real cross-table FK can be added in a later migration.

RELATIONSHIP TO study_store.py
    ImagingStudy.id is the SAME uuid4 string used as the key into
    study_store.StudyStore and into the on-disk asset layout under
    STUDY_STORE_PATH / an AssetStore backend (see asset_storage.py). This
    table does not replace the file-based store - large arrays never belong
    in a relational column (see ARCHITECTURE_AUDIT.md / master spec section
    3) - it is the structured index and provenance/job/measurement layer
    that sits in front of it.

NO CLINICAL FABRICATION
    Nothing in this schema stores a diagnosis, a disease label, or a
    confidence score for anything the application cannot actually compute.
    RegionOfInterest.label is either a clinician's own free-text annotation
    or left null for a deterministic segmentation result; it is never
    auto-populated with a disease name.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime, ForeignKey, Index,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Imaging hierarchy: ImagingStudy -> ImagingSeries
# ---------------------------------------------------------------------------

class ImagingStudy(Base):
    """One imported CT study. id matches the study_store.py key exactly.

    STATUS VALUES: imported -> segmented -> analyzed, or failed at any step.
    This is a simple forward progression today because ingestion is
    synchronous; it is not a job queue (see ProcessingJob for that).
    """
    __tablename__ = "imaging_studies"

    id = Column(String(36), primary_key=True, default=_uuid)
    case_id = Column(Integer, nullable=True, index=True)  # logical ref -> db.py cases.id
    owner_doctor_id = Column(Integer, nullable=False, index=True)  # logical ref -> db.py doctors.id

    status = Column(String(32), nullable=False, default="imported")
    modality = Column(String(16), nullable=True)
    slice_count = Column(Integer, nullable=True)
    rows = Column(Integer, nullable=True)
    columns = Column(Integer, nullable=True)

    # Content-hash of a strided sample of the HU volume (see
    # tools' reprocess/dedup scripts this formalizes). Lets a re-import of
    # the same series be detected without re-decoding every DICOM file.
    asset_hash = Column(String(64), nullable=True, index=True)

    storage_backend = Column(String(16), nullable=False, default="local")
    storage_key = Column(String(255), nullable=False)  # key/prefix under the AssetStore

    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    series = relationship("ImagingSeries", back_populates="study",
                           cascade="all, delete-orphan")
    jobs = relationship("ProcessingJob", back_populates="study",
                         cascade="all, delete-orphan")
    regions = relationship("RegionOfInterest", back_populates="study",
                            cascade="all, delete-orphan")
    measurements = relationship("Measurement", back_populates="study",
                                 cascade="all, delete-orphan")
    annotations = relationship("Annotation", back_populates="study",
                                cascade="all, delete-orphan")


class ImagingSeries(Base):
    """Technical acquisition metadata for one series within a study.

    The current ingestion pipeline (api/index.py: validate_dicom_series ->
    order_slices_spatially -> build_volume) produces exactly one series per
    study, so exactly one row is created per ImagingStudy today. The 1:many
    relationship exists so a future multi-series import does not require a
    schema change - it is not itself an implemented capability.
    """
    __tablename__ = "imaging_series"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("imaging_studies.id"), nullable=False, index=True)

    series_instance_uid = Column(String(128), nullable=True)
    slice_count = Column(Integer, nullable=True)
    rows = Column(Integer, nullable=True)
    columns = Column(Integer, nullable=True)
    pixel_spacing_row_mm = Column(Float, nullable=True)
    pixel_spacing_col_mm = Column(Float, nullable=True)
    slice_spacing_mm = Column(Float, nullable=True)
    orientation_reliable = Column(Boolean, nullable=False, default=False)
    reconstruction_kernel = Column(String(64), nullable=True)
    hu_available = Column(Boolean, nullable=False, default=False)
    fallback_ordering_used = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    study = relationship("ImagingStudy", back_populates="series")


# ---------------------------------------------------------------------------
# Processing jobs
# ---------------------------------------------------------------------------

class ProcessingJob(Base):
    """Bookkeeping for one unit of imaging computation.

    HONEST LIMITATION: this deployment (Vercel serverless, see
    ARCHITECTURE_AUDIT.md section 4) has no long-running worker process, so
    every job today is created and completed synchronously within the same
    request that does the work - QUEUED is set and then immediately
    transitioned, never actually queued for a separate process to pick up.
    This table exists so that timing, failure, and parameter/version
    provenance are captured uniformly, and so a real async worker can be
    dropped in later (see ARCHITECTURE_AUDIT.md section 7) without a schema
    change - not because jobs are actually asynchronous today.
    """
    __tablename__ = "processing_jobs"

    JOB_TYPES = ("IMPORT", "SEGMENTATION", "MESH_GENERATION",
                 "QUANTITATIVE_ANALYSIS", "DENSITOMETRY")
    STATUSES = ("QUEUED", "RUNNING", "COMPLETED", "FAILED")

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("imaging_studies.id"), nullable=False, index=True)

    job_type = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="QUEUED")
    progress = Column(Float, nullable=False, default=0.0)

    method = Column(String(128), nullable=True)
    method_version = Column(String(32), nullable=True)
    parameters_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    study = relationship("ImagingStudy", back_populates="jobs")


# ---------------------------------------------------------------------------
# Regions of interest, measurements, annotations
# ---------------------------------------------------------------------------

class RegionOfInterest(Base):
    """A locatable region, either clinician-drawn or algorithmically derived.

    SOURCE governs what `label` may legitimately contain:
      - 'clinician_annotation': a doctor's own free-text label.
      - 'deterministic_segmentation': label is always null. The region's
        physical properties (volume, HU stats, centroid) come from
        density_regions.py's clustering; this row is the persisted,
        clinician-referenceable form of one of those regions, not a new
        computation.
    Never store a disease name, pattern name, or confidence score unless a
    validated model produced it and a genuine confidence value exists - the
    application has neither today (see density_regions.py's own module
    documentation).
    """
    __tablename__ = "regions_of_interest"

    SOURCES = ("clinician_annotation", "deterministic_segmentation")

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("imaging_studies.id"), nullable=False, index=True)

    source = Column(String(32), nullable=False)
    label = Column(String(255), nullable=True)

    centroid_x_mm = Column(Float, nullable=True)
    centroid_y_mm = Column(Float, nullable=True)
    centroid_z_mm = Column(Float, nullable=True)
    bbox_json = Column(Text, nullable=True)
    volume_ml = Column(Float, nullable=True)
    mean_hu = Column(Float, nullable=True)
    median_hu = Column(Float, nullable=True)
    laterality = Column(String(16), nullable=True)
    zone = Column(String(32), nullable=True)

    created_by_doctor_id = Column(Integer, nullable=True)  # null for deterministic regions
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    provenance_json = Column(Text, nullable=True)

    study = relationship("ImagingStudy", back_populates="regions")


class Measurement(Base):
    """A clinician-created measurement anchored to patient-space coordinates.

    Geometry is always stored in millimetres in patient space (never screen
    pixels - see master spec section 31): `geometry_json` holds a list of
    [x_mm, y_mm, z_mm] points, whose count and meaning depend on
    measurement_type (1 point for point_hu, 2 for distance/diameter, N for
    an area/volume boundary).
    """
    __tablename__ = "measurements"

    TYPES = ("point_hu", "distance", "longest_diameter",
              "perpendicular_diameter", "area", "volume")

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("imaging_studies.id"), nullable=False, index=True)

    measurement_type = Column(String(32), nullable=False)
    geometry_json = Column(Text, nullable=False)
    value = Column(Float, nullable=False)
    units = Column(String(16), nullable=False)

    mean_hu = Column(Float, nullable=True)
    median_hu = Column(Float, nullable=True)
    hu_stddev = Column(Float, nullable=True)

    created_by_doctor_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    provenance_json = Column(Text, nullable=True)

    study = relationship("ImagingStudy", back_populates="measurements")


class Annotation(Base):
    """A clinician's free-text note anchored to one or more patient-space
    points on the 3D model or a 2D slice - a single point is a "pin"; more
    than one is a freehand highlight stroke drawn across the mesh surface.

    COORDINATE TRUST: for a 2D-slice pin, the server derives points_json
    from a client-sent VOXEL INDEX via VolumeGeometry.to_world (see
    index.py's _voxel_to_world_and_hu) - the client never sends millimetre
    coordinates directly for that path. For a 3D-mesh pin or stroke, the
    client sends the millimetre point(s) directly, and this is intentional
    rather than a relaxation of that rule: the mesh vertices the browser
    raycasts against were themselves computed server-side with the true
    physical affine (mesh_reconstruction.build_lung_mesh), so any point on
    that surface - including a raycast hit interpolated between vertices -
    is already real patient-space geometry, not a screen-pixel guess. The
    same trust boundary already applies to the viewer's existing (session-
    only, unsaved) 3D distance-measurement tool. Server-side validation
    still bounds every coordinate to a plausible physical range (see
    index.py's _validate_points_mm) to catch a malformed/corrupted request.
    """
    __tablename__ = "annotations"

    id = Column(String(36), primary_key=True, default=_uuid)
    study_id = Column(String(36), ForeignKey("imaging_studies.id"), nullable=False, index=True)

    points_json = Column(Text, nullable=False)  # JSON list of >=1 [x_mm, y_mm, z_mm]
    color = Column(String(7), nullable=False, default="#e8a33d")  # #RRGGBB
    text = Column(Text, nullable=False)

    created_by_doctor_id = Column(Integer, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    study = relationship("ImagingStudy", back_populates="annotations")


Index("ix_measurements_study_type", Measurement.study_id, Measurement.measurement_type)
Index("ix_roi_study_source", RegionOfInterest.study_id, RegionOfInterest.source)
