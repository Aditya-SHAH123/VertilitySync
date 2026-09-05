"""
Ground-truth tests for the SQLAlchemy imaging models (api/models.py,
api/db_engine.py, api/measurements.py).

Every test uses a throwaway SQLite file so nothing here touches
instance/imaging.db. No real patient data anywhere.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))

import db_engine  # noqa: E402
import measurements as m  # noqa: E402
from models import ProcessingJob, RegionOfInterest, Measurement  # noqa: E402


class ModelsTestCase(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)  # sqlite creates it fresh
        self.db_path = path
        self.url = f"sqlite:///{path}"
        os.environ["IMAGING_DATABASE_URL"] = self.url
        db_engine.reset_models_db(self.url)

    def tearDown(self):
        db_engine.dispose_engine()
        os.environ.pop("IMAGING_DATABASE_URL", None)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)


class TestImagingStudyRoundTrip(ModelsTestCase):
    def test_upsert_creates_then_updates(self):
        sid = "study-1"
        m.upsert_imaging_study(sid, owner_doctor_id=7, modality="CT",
                                slice_count=300, rows=512, columns=512,
                                asset_hash="abc123")
        with db_engine.get_session() as s:
            from models import ImagingStudy
            study = s.get(ImagingStudy, sid)
            self.assertEqual(study.owner_doctor_id, 7)
            self.assertEqual(study.slice_count, 300)
            self.assertEqual(study.status, "imported")

        # Upsert again with a different slice count - must update, not duplicate.
        m.upsert_imaging_study(sid, owner_doctor_id=7, modality="CT",
                                slice_count=301, rows=512, columns=512,
                                asset_hash="abc123")
        with db_engine.get_session() as s:
            from models import ImagingStudy
            rows = s.query(ImagingStudy).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].slice_count, 301)

    def test_status_update(self):
        sid = "study-2"
        m.upsert_imaging_study(sid, owner_doctor_id=1)
        m.update_study_status(sid, "segmented")
        with db_engine.get_session() as s:
            from models import ImagingStudy
            self.assertEqual(s.get(ImagingStudy, sid).status, "segmented")

    def test_status_update_unknown_study_raises(self):
        with self.assertRaises(KeyError):
            m.update_study_status("does-not-exist", "segmented")

    def test_series_created_with_study_link(self):
        sid = "study-3"
        m.upsert_imaging_study(sid, owner_doctor_id=1)
        series_id = m.add_series(sid, series_instance_uid="1.2.3", slice_count=250,
                                  rows=512, columns=512, pixel_spacing_row_mm=0.7,
                                  pixel_spacing_col_mm=0.7, slice_spacing_mm=1.25,
                                  orientation_reliable=True, hu_available=True)
        with db_engine.get_session() as s:
            from models import ImagingSeries
            series = s.get(ImagingSeries, series_id)
            self.assertEqual(series.study_id, sid)
            self.assertTrue(series.orientation_reliable)
            self.assertTrue(series.hu_available)
            self.assertFalse(series.fallback_ordering_used)


class TestProcessingJobs(ModelsTestCase):
    def test_full_lifecycle_completed(self):
        sid = "study-job-1"
        m.upsert_imaging_study(sid, owner_doctor_id=1)
        job_id = m.start_job(sid, "SEGMENTATION", method="threshold_cc",
                              method_version="1.2.0", parameters={"min_component_ml": 50})
        with db_engine.get_session() as s:
            job = s.get(ProcessingJob, job_id)
            self.assertEqual(job.status, "RUNNING")
            self.assertIsNotNone(job.started_at)
            self.assertIsNone(job.completed_at)

        m.complete_job(job_id, progress=1.0)
        with db_engine.get_session() as s:
            job = s.get(ProcessingJob, job_id)
            self.assertEqual(job.status, "COMPLETED")
            self.assertIsNotNone(job.completed_at)

    def test_full_lifecycle_failed(self):
        sid = "study-job-2"
        m.upsert_imaging_study(sid, owner_doctor_id=1)
        job_id = m.start_job(sid, "MESH_GENERATION")
        m.fail_job(job_id, "marching cubes raised ValueError: empty mask")
        with db_engine.get_session() as s:
            job = s.get(ProcessingJob, job_id)
            self.assertEqual(job.status, "FAILED")
            self.assertIn("empty mask", job.error_message)

    def test_invalid_job_type_rejected(self):
        m.upsert_imaging_study("study-job-3", owner_doctor_id=1)
        with self.assertRaises(ValueError):
            m.start_job("study-job-3", "NOT_A_REAL_JOB_TYPE")

    def test_list_jobs_ordered_most_recent_first(self):
        sid = "study-job-4"
        m.upsert_imaging_study(sid, owner_doctor_id=1)
        j1 = m.start_job(sid, "IMPORT")
        m.complete_job(j1)
        j2 = m.start_job(sid, "SEGMENTATION")
        m.complete_job(j2)
        jobs = m.list_jobs(sid)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], j2)
        self.assertEqual(jobs[1]["id"], j1)

    def test_complete_unknown_job_raises(self):
        with self.assertRaises(KeyError):
            m.complete_job("nonexistent-job-id")


class TestRegionsOfInterest(ModelsTestCase):
    def setUp(self):
        super().setUp()
        self.sid = "study-roi-1"
        m.upsert_imaging_study(self.sid, owner_doctor_id=1)

    def test_deterministic_region_cannot_carry_a_label(self):
        with self.assertRaises(ValueError):
            m.create_region(self.sid, source="deterministic_segmentation",
                             label="looks like honeycombing")

    def test_clinician_region_round_trip(self):
        rid = m.create_region(
            self.sid, source="clinician_annotation", label="watch this area",
            centroid_mm=(10.0, 20.0, -30.0), bbox=[0, 0, 0, 10, 10, 10],
            volume_ml=4.2, mean_hu=-750.0, laterality="left", zone="upper",
            created_by_doctor_id=5, provenance={"source": "clinician_click"},
        )
        regions = m.list_regions(self.sid)
        self.assertEqual(len(regions), 1)
        r = regions[0]
        self.assertEqual(r["id"], rid)
        self.assertEqual(r["label"], "watch this area")
        self.assertEqual(r["centroid_mm"], [10.0, 20.0, -30.0])
        self.assertEqual(r["bbox"], [0, 0, 0, 10, 10, 10])
        self.assertEqual(r["provenance"], {"source": "clinician_click"})

    def test_deterministic_region_has_no_label_and_null_creator(self):
        rid = m.create_region(
            self.sid, source="deterministic_segmentation",
            centroid_mm=(0.0, 0.0, 0.0), volume_ml=178.4, mean_hu=-920.0,
            provenance={"source": "density_regions", "method_version": "1.0.0"},
        )
        regions = m.list_regions(self.sid, source="deterministic_segmentation")
        self.assertEqual(len(regions), 1)
        self.assertIsNone(regions[0]["label"])
        self.assertIsNone(regions[0]["created_by_doctor_id"])

    def test_filter_by_source(self):
        m.create_region(self.sid, source="clinician_annotation", label="a",
                         created_by_doctor_id=1)
        m.create_region(self.sid, source="deterministic_segmentation")
        m.create_region(self.sid, source="deterministic_segmentation")
        self.assertEqual(len(m.list_regions(self.sid, source="clinician_annotation")), 1)
        self.assertEqual(len(m.list_regions(self.sid, source="deterministic_segmentation")), 2)
        self.assertEqual(len(m.list_regions(self.sid)), 3)

    def test_delete_requires_matching_creator(self):
        rid = m.create_region(self.sid, source="clinician_annotation", label="a",
                               created_by_doctor_id=1)
        with self.assertRaises(PermissionError):
            m.delete_region(rid, created_by_doctor_id=2)
        m.delete_region(rid, created_by_doctor_id=1)
        self.assertEqual(len(m.list_regions(self.sid)), 0)

    def test_invalid_source_rejected(self):
        with self.assertRaises(ValueError):
            m.create_region(self.sid, source="ai_guess")


class TestMeasurements(ModelsTestCase):
    def setUp(self):
        super().setUp()
        self.sid = "study-meas-1"
        m.upsert_imaging_study(self.sid, owner_doctor_id=1)

    def test_distance_round_trip_patient_space_mm(self):
        mid = m.create_measurement(
            self.sid, "distance",
            geometry_mm=[[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
            value=10.0, units="mm", created_by_doctor_id=3,
            provenance={"source": "clinician_measurement"},
        )
        rows = m.list_measurements(self.sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], mid)
        self.assertEqual(rows[0]["value"], 10.0)
        self.assertEqual(rows[0]["units"], "mm")
        self.assertEqual(rows[0]["geometry_mm"], [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        self.assertEqual(rows[0]["created_by_doctor_id"], 3)

    def test_point_hu_with_density_stats(self):
        m.create_measurement(
            self.sid, "point_hu", geometry_mm=[[1.0, 2.0, 3.0]],
            value=-850.0, units="HU", created_by_doctor_id=1, mean_hu=-850.0,
        )
        rows = m.list_measurements(self.sid)
        self.assertEqual(rows[0]["mean_hu"], -850.0)

    def test_invalid_type_rejected(self):
        with self.assertRaises(ValueError):
            m.create_measurement(self.sid, "diagnosis_confidence",
                                  geometry_mm=[[0, 0, 0]], value=1.0,
                                  units="", created_by_doctor_id=1)

    def test_empty_geometry_rejected(self):
        with self.assertRaises(ValueError):
            m.create_measurement(self.sid, "point_hu", geometry_mm=[],
                                  value=1.0, units="HU", created_by_doctor_id=1)

    def test_delete_requires_matching_creator(self):
        mid = m.create_measurement(self.sid, "distance",
                                    geometry_mm=[[0, 0, 0], [5, 0, 0]],
                                    value=5.0, units="mm", created_by_doctor_id=1)
        with self.assertRaises(PermissionError):
            m.delete_measurement(mid, created_by_doctor_id=99)
        m.delete_measurement(mid, created_by_doctor_id=1)
        self.assertEqual(len(m.list_measurements(self.sid)), 0)


class TestAnnotations(ModelsTestCase):
    def setUp(self):
        super().setUp()
        self.sid = "study-annot-1"
        m.upsert_imaging_study(self.sid, owner_doctor_id=1)

    def test_note_with_single_point_is_a_pin(self):
        aid = m.create_annotation(self.sid, "Dense region here", created_by_doctor_id=2,
                                   points_mm=[[5.0, 5.0, 5.0]])
        rows = m.list_annotations(self.sid)
        self.assertEqual(rows[0]["id"], aid)
        self.assertEqual(rows[0]["points_mm"], [[5.0, 5.0, 5.0]])
        self.assertEqual(rows[0]["kind"], "pin")
        self.assertEqual(rows[0]["color"], "#e8a33d")

    def test_note_with_multiple_points_is_a_stroke(self):
        pts = [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        m.create_annotation(self.sid, "Suspicious ridge", created_by_doctor_id=2,
                             points_mm=pts, color="#3d7be8")
        rows = m.list_annotations(self.sid)
        self.assertEqual(rows[0]["points_mm"], pts)
        self.assertEqual(rows[0]["kind"], "stroke")
        self.assertEqual(rows[0]["color"], "#3d7be8")

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            m.create_annotation(self.sid, "   ", created_by_doctor_id=1, points_mm=[[0, 0, 0]])

    def test_no_points_rejected(self):
        with self.assertRaises(ValueError):
            m.create_annotation(self.sid, "note", created_by_doctor_id=1, points_mm=[])

    def test_delete_requires_matching_creator(self):
        aid = m.create_annotation(self.sid, "note", created_by_doctor_id=1, points_mm=[[0, 0, 0]])
        with self.assertRaises(PermissionError):
            m.delete_annotation(aid, created_by_doctor_id=2)
        m.delete_annotation(aid, created_by_doctor_id=1)
        self.assertEqual(len(m.list_annotations(self.sid)), 0)


class TestCascadeDelete(ModelsTestCase):
    def test_deleting_study_removes_children(self):
        sid = "study-cascade-1"
        m.upsert_imaging_study(sid, owner_doctor_id=1)
        m.add_series(sid, slice_count=10)
        job_id = m.start_job(sid, "IMPORT")
        m.create_region(sid, source="deterministic_segmentation")
        m.create_measurement(sid, "point_hu", geometry_mm=[[0, 0, 0]],
                              value=-800.0, units="HU", created_by_doctor_id=1)
        m.create_annotation(sid, "note", created_by_doctor_id=1, points_mm=[[0, 0, 0]])

        with db_engine.get_session() as s:
            from models import ImagingStudy
            study = s.get(ImagingStudy, sid)
            s.delete(study)

        with db_engine.get_session() as s:
            from models import ImagingSeries
            self.assertEqual(s.query(ImagingSeries).count(), 0)
            self.assertEqual(s.query(ProcessingJob).count(), 0)
            self.assertEqual(s.query(RegionOfInterest).count(), 0)
            self.assertEqual(s.query(Measurement).count(), 0)


if __name__ == "__main__":
    import sys
    result = unittest.main(exit=False).result
    print(f'\n{result.testsRun - len(result.failures) - len(result.errors)} passed, '
          f'{len(result.failures) + len(result.errors)} failed')
    sys.exit(0 if result.wasSuccessful() else 1)
