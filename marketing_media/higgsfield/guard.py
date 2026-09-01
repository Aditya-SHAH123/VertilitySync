"""
Privacy boundary between the medical imaging pipeline and Higgsfield.

Higgsfield is a PUBLIC MARKETING MEDIA service. It is not part of the medical
imaging engine and must never receive patient data of any kind.

This module is the single choke point every Higgsfield request passes
through. It is deliberately built as an ALLOW-LIST, not a block-list:

  * Only prompts drawn from the reviewed catalog in
    marketing_media/prompts/approved_prompts.json may be submitted. A
    free-form prompt assembled at runtime is refused outright, so a bug or an
    injected string elsewhere in the app cannot become a Higgsfield request.
  * Only `str` prompts are accepted. Passing an ndarray, bytes, a file
    handle, a path, or a dict is refused by type before any content check -
    this is what structurally prevents a DICOM volume or pixel buffer from
    ever being serialized into a request.
  * A block-list of clinical/PHI vocabulary runs on top of the allow-list as
    defence in depth, so a mistakenly-edited catalog entry containing patient
    terminology still fails closed.

Generated output is marketing/educational media. It is never medical
evidence, never displayed inside the clinical workspace, and never derived
from a real study.
"""

import json
import os
import re

PROMPTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "prompts", "approved_prompts.json")


class HiggsfieldPrivacyError(Exception):
    """Raised when a request would violate the medical-data boundary."""


class HiggsfieldConfigError(Exception):
    """Raised when the integration is not configured (e.g. missing API key)."""


# Vocabulary that must never appear in a marketing generation request.
# Matched case-insensitively on word boundaries.
FORBIDDEN_TERMS = [
    # Direct identifiers
    "patient name", "patientname", "patient id", "patientid", "mrn",
    "medical record number", "date of birth", "dateofbirth", "birthdate",
    "accession", "accession number", "social security", "ssn",
    # Clinical artifacts
    "dicom", "\\.dcm", "pixeldata", "pixel data", "sopinstanceuid",
    "studyinstanceuid", "seriesinstanceuid", "imagepositionpatient",
    "radiology report", "clinical note", "doctor note", "case file",
    "medical record", "chart note", "pathology report", "referring physician",
    # Diagnostic claims that must not appear in marketing media
    "diagnosis", "diagnostic result", "malignant", "biopsy result",
]

FORBIDDEN_RE = re.compile(r"(?<!\w)(" + "|".join(FORBIDDEN_TERMS) + r")(?!\w)", re.IGNORECASE)

# Phrases every approved visual prompt must carry, so generated media cannot
# drift into fabricated clinical content.
REQUIRED_NEGATIVE_PHRASES = ["no text", "no patient identifiers"]


def load_approved_prompts(path=None):
    with open(path or PROMPTS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {entry["id"]: entry for entry in data["prompts"]}


def assert_no_phi(value, field="input"):
    """Type-then-content check. Refuses anything that is not a plain string,
    which is what stops binary imaging data structurally rather than by
    pattern matching."""
    if not isinstance(value, str):
        raise HiggsfieldPrivacyError(
            f"{field} must be a plain string; got {type(value).__name__}. "
            f"Binary or structured data (imaging arrays, DICOM datasets, file "
            f"handles, paths) may never be sent to Higgsfield."
        )
    match = FORBIDDEN_RE.search(value)
    if match:
        raise HiggsfieldPrivacyError(
            f"{field} contains forbidden clinical/patient terminology: "
            f"'{match.group(0)}'. Higgsfield is for nonclinical marketing media only."
        )
    return value


def validate_approved_prompt(prompt_id, prompts=None):
    """Resolves a catalog id to its reviewed prompt text, re-validating the
    catalog entry itself before returning it."""
    catalog = prompts if prompts is not None else load_approved_prompts()
    if prompt_id not in catalog:
        raise HiggsfieldPrivacyError(
            f"'{prompt_id}' is not an approved prompt id. Only reviewed prompts in "
            f"approved_prompts.json may be generated; free-form prompts are refused."
        )
    entry = catalog[prompt_id]
    text = entry["prompt"]
    assert_no_phi(text, field=f"approved prompt '{prompt_id}'")

    lowered = text.lower()
    missing = [p for p in REQUIRED_NEGATIVE_PHRASES if p not in lowered]
    if missing:
        raise HiggsfieldPrivacyError(
            f"Approved prompt '{prompt_id}' is missing required safety phrase(s): {missing}."
        )
    return entry


def get_api_key():
    """Reads the key from the environment only. The key is never rendered
    into a template, returned by an endpoint, or logged."""
    key = os.environ.get("HIGGSFIELD_API_KEY")
    if not key:
        raise HiggsfieldConfigError(
            "HIGGSFIELD_API_KEY is not set. Media generation is a server-side, "
            "offline build step; set the key in the environment before running "
            "marketing_media/generate.py."
        )
    return key


def assert_not_clinical_source(obj):
    """Explicit guard for call sites that handle study data. Any attempt to
    route an object that looks like imaging state into the marketing pipeline
    raises immediately."""
    clinical_markers = ("hu_volume", "pixel_array", "PixelData", "SOPInstanceUID",
                        "segmentation", "geometry", "hu_available_per_slice")
    if isinstance(obj, dict):
        found = [k for k in clinical_markers if k in obj]
        if found:
            raise HiggsfieldPrivacyError(
                f"Refusing to pass clinical study state to Higgsfield (found keys: {found})."
            )
    if hasattr(obj, "shape") and hasattr(obj, "dtype"):
        raise HiggsfieldPrivacyError(
            "Refusing to pass an array-like object (imaging volume) to Higgsfield."
        )
    return obj
