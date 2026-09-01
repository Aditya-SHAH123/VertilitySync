"""
Generates purely synthetic DICOM CT series for local pipeline testing.
No real patient data is used or referenced anywhere in this file.
"""
import os
import numpy as np
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid


def make_series(out_dir, n_slices=8, rows=64, cols=64, with_geometry=True,
                 with_rescale=True, series_uid=None, study_uid=None,
                 slice_thickness=2.5, start_z=0.0):
    os.makedirs(out_dir, exist_ok=True)
    series_uid = series_uid or generate_uid()
    study_uid = study_uid or generate_uid()
    paths = []

    for i in range(n_slices):
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.2'  # CT Image Storage
        file_meta.MediaStorageSOPInstanceUID = generate_uid()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = FileDataset(None, {}, file_meta=file_meta, preamble=b'\x00' * 128)
        ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
        ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
        ds.Modality = 'CT'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.InstanceNumber = i + 1

        ds.Rows = rows
        ds.Columns = cols
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = 'MONOCHROME2'
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1

        ds.PixelSpacing = [0.7, 0.7]
        ds.SliceThickness = slice_thickness

        z = start_z + i * slice_thickness
        if with_geometry:
            ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
            ds.ImagePositionPatient = [-22.4, -22.4, z]
        ds.SliceLocation = z

        if with_rescale:
            ds.RescaleSlope = 1.0
            ds.RescaleIntercept = -1024.0

        ds.Manufacturer = 'SyntheticGen'
        ds.ManufacturerModelName = 'TestPhantom'
        ds.KVP = 120

        # Simple synthetic pattern: a bright disc that grows/shrinks per slice,
        # over a dark background, entirely procedurally generated.
        yy, xx = np.mgrid[0:rows, 0:cols]
        cx, cy = cols / 2, rows / 2
        radius = 10 + 3 * np.sin(i / 2.0)
        disc = ((xx - cx) ** 2 + (yy - cy) ** 2) < radius ** 2
        pixel_array = np.full((rows, cols), -1000, dtype=np.int16)  # air
        pixel_array[disc] = 200  # soft-tissue-ish raw value before rescale... (already close to HU here since slope=1, intercept=-1024 means raw stored = HU+1024)
        # store raw values such that HU = raw*1 - 1024
        raw = (pixel_array.astype(np.int32) + 1024).astype(np.int16)
        ds.PixelData = raw.tobytes()

        ds.is_little_endian = True
        ds.is_implicit_VR = False

        path = os.path.join(out_dir, f'slice_{i:03d}.dcm')
        ds.save_as(path, write_like_original=False)
        paths.append(path)

    return paths, series_uid, study_uid


def make_lung_phantom_series(out_dir, n_slices=30, rows=100, cols=100,
                              pixel_spacing=1.4, slice_thickness=4.0,
                              lung_slice_range=(3, 27), series_uid=None, study_uid=None):
    """
    Generates a purely synthetic, geometric chest-CT-like phantom: a circular
    soft-tissue "body" silhouette in air, containing two circular
    lower-density "lung" regions offset left/right of center. HU values are
    schematic (air ~ -1000, soft tissue ~ 40, lung parenchyma ~ -800) chosen
    to exercise the rule-based segmentation thresholds, not to represent real
    anatomy or any real patient. No real imaging data is used anywhere here.
    """
    os.makedirs(out_dir, exist_ok=True)
    series_uid = series_uid or generate_uid()
    study_uid = study_uid or generate_uid()
    paths = []

    cx, cy = cols / 2.0, rows / 2.0
    body_radius_mm = 55.0
    lung_radius_mm = 20.0
    lung_offset_mm = 28.0
    yy, xx = np.mgrid[0:rows, 0:cols]
    dx_mm = (xx - cx) * pixel_spacing
    dy_mm = (yy - cy) * pixel_spacing

    body = (dx_mm ** 2 + dy_mm ** 2) <= body_radius_mm ** 2
    left_lung = ((dx_mm - lung_offset_mm) ** 2 + dy_mm ** 2) <= lung_radius_mm ** 2   # larger world X = patient LEFT
    right_lung = ((dx_mm + lung_offset_mm) ** 2 + dy_mm ** 2) <= lung_radius_mm ** 2  # smaller world X = patient RIGHT

    lo, hi = lung_slice_range
    for i in range(n_slices):
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.2'
        file_meta.MediaStorageSOPInstanceUID = generate_uid()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = FileDataset(None, {}, file_meta=file_meta, preamble=b'\x00' * 128)
        ds.SOPClassUID = file_meta.MediaStorageSOPClassUID
        ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
        ds.Modality = 'CT'
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.InstanceNumber = i + 1

        ds.Rows = rows
        ds.Columns = cols
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = 'MONOCHROME2'
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1

        ds.PixelSpacing = [pixel_spacing, pixel_spacing]
        ds.SliceThickness = slice_thickness

        z = i * slice_thickness
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.ImagePositionPatient = [0.0, 0.0, z]
        ds.SliceLocation = z

        ds.RescaleSlope = 1.0
        ds.RescaleIntercept = -1024.0

        ds.Manufacturer = 'SyntheticGen'
        ds.ManufacturerModelName = 'LungPhantom'
        ds.KVP = 120

        hu = np.full((rows, cols), -1000.0, dtype=np.float32)
        hu[body] = 40.0
        if lo <= i < hi:
            hu[left_lung] = -800.0
            hu[right_lung] = -800.0

        raw = (hu.astype(np.int32) + 1024).astype(np.int16)
        ds.PixelData = raw.tobytes()

        ds.is_little_endian = True
        ds.is_implicit_VR = False

        path = os.path.join(out_dir, f'slice_{i:03d}.dcm')
        ds.save_as(path, write_like_original=False)
        paths.append(path)

    return paths, series_uid, study_uid


if __name__ == '__main__':
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else '/tmp/synthetic_dicom'
    paths, series_uid, study_uid = make_series(out)
    print(f'Wrote {len(paths)} synthetic DICOM files to {out}')
