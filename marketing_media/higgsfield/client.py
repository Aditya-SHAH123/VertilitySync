
"""
Server-side Higgsfield client for PUBLIC marketing media.

This is an OFFLINE BUILD STEP, not a request-time dependency. The website
never calls Higgsfield when a visitor loads a page; it reads whatever is
already listed in marketing_media/manifests/assets.json. That keeps page
loads fast and avoids per-visit API cost.

Pipeline:
    approved prompt id
        -> guard.validate_approved_prompt (allow-list + PHI check)
        -> submit_generation (server-side, API key from env)
        -> human review
        -> optimize/compress
        -> manifest entry
        -> website asset

The API key is read from the environment inside guard.get_api_key() and is
never returned, rendered, or logged by this module.
"""

import json
import os

from .guard import (
    validate_approved_prompt, get_api_key, assert_no_phi,
    assert_not_clinical_source, HiggsfieldPrivacyError, HiggsfieldConfigError,
)

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "manifests", "assets.json")


def build_request(prompt_id, extra_context=None):
    """Assembles a validated generation request payload.

    `extra_context` is intentionally restricted: it may only be a short
    plain-string stylistic note, and it passes through the same PHI check as
    the prompt itself. There is no parameter through which imaging data can
    reach this payload.
    """
    entry = validate_approved_prompt(prompt_id)

    if extra_context is not None:
        assert_not_clinical_source(extra_context)
        assert_no_phi(extra_context, field="extra_context")
        if len(extra_context) > 300:
            raise HiggsfieldPrivacyError(
                "extra_context is limited to 300 characters of stylistic direction."
            )

    prompt_text = entry["prompt"]
    if extra_context:
        prompt_text = f"{prompt_text} {extra_context}"

    return {
        "prompt_id": prompt_id,
        "prompt": prompt_text,
        "media_type": entry["media_type"],
        "aspect_ratio": entry.get("aspect_ratio", "16:9"),
        "section": entry.get("section"),
    }


def submit_generation(prompt_id, extra_context=None, dry_run=True):
    """Validates and (optionally) submits a generation request.

    `dry_run=True` is the default on purpose: it performs every validation
    step and returns the exact payload that WOULD be sent, without spending
    API credit. Actual submission requires an explicit `dry_run=False` and a
    configured HIGGSFIELD_API_KEY.

    NOTE: the concrete transport call is intentionally left as the single
    integration point below. This project has not run a live generation, so
    no response-shape handling is claimed to be verified.
    """
    payload = build_request(prompt_id, extra_context=extra_context)

    if dry_run:
        return {"status": "DRY_RUN", "payload": payload,
                "note": "Validated only. No API call was made and no credit was spent."}

    get_api_key()  # raises HiggsfieldConfigError if unset; key never leaves this scope
    raise HiggsfieldConfigError(
        "Live Higgsfield submission is not wired to a transport in this repository. "
        "The validated payload is available via build_request(); connect it to the "
        "Higgsfield API here, then record the reviewed result with add_manifest_entry(). "
        "This function deliberately refuses rather than pretending a generation occurred."
    )


# ---------------------------------------------------------------------------
# Asset manifest - what the public website actually reads
# ---------------------------------------------------------------------------

def load_manifest(path=None):
    p = path or MANIFEST_PATH
    if not os.path.exists(p):
        return {"version": "1.0.0", "assets": []}
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def add_manifest_entry(prompt_id, asset_path, media_type, poster=None,
                        reviewed_by=None, notes=None, path=None):
    """Records a human-reviewed, optimized asset so the website can use it.

    `reviewed_by` is required: an asset cannot enter the manifest (and
    therefore cannot appear on the site) without an explicit review record.
    """
    if not reviewed_by:
        raise ValueError("reviewed_by is required - assets must be human-reviewed before publication.")
    validate_approved_prompt(prompt_id)

    manifest = load_manifest(path)
    manifest["assets"] = [a for a in manifest["assets"] if a["prompt_id"] != prompt_id]
    manifest["assets"].append({
        "prompt_id": prompt_id,
        "asset_path": asset_path,
        "media_type": media_type,
        "poster": poster,
        "reviewed_by": reviewed_by,
        "notes": notes,
        "classification": "public-marketing-media",
        "derived_from_patient_data": False,
    })
    with open(path or MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def get_asset(prompt_id, path=None):
    """Returns the reviewed asset for a prompt id, or None if nothing has
    been generated and approved yet. The website falls back to its built-in
    procedural visuals when this returns None."""
    for asset in load_manifest(path).get("assets", []):
        if asset["prompt_id"] == prompt_id:
            return asset
    return None
