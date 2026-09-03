"""
Optional AI rewriting of clinician notes for grammar/clarity/professionalism
only - never for content. This module never runs automatically; every call
is a doctor clicking "Polish with AI" on text they already wrote, and the
result is only ever a *suggestion* the doctor separately accepts or rejects
(see api/patients.py's original_content/current_content split and the
/api/notes/polish route in api/index.py).

PRIVACY: only the note text itself is ever sent to the provider - never a
DICOM file, image, volume, mesh, or any other field from the patient record.

PROVIDER: uses the Groq API via GROQ_API_KEY, which already existed in this
project's environment configuration before this feature (see .env / db.py's
docstring). No second AI provider is introduced. If the key is unset, or the
request fails for any reason, this returns a NOT_CONFIGURED/FAIL status -
note-taking itself must keep working either way.
"""

import os

SYSTEM_INSTRUCTION = (
    "You are editing a clinician's note only for grammar, clarity, "
    "organization, concision, and professional wording.\n\n"
    "Preserve the exact medical meaning.\n\n"
    "Do not add, infer, remove, strengthen, or weaken clinical findings. "
    "Do not create diagnoses. Do not invent measurements, symptoms, "
    "medications, dates, recommendations, or interpretations.\n\n"
    "Preserve all anatomical locations, laterality, numbers, qualifiers, "
    "uncertainty, and severity exactly.\n\n"
    "If the original note expresses uncertainty, preserve that uncertainty.\n\n"
    "Return only the rewritten note."
)

MODEL = "llama-3.1-8b-instant"
MAX_INPUT_CHARS = 4000  # a clinical note, not a document - keeps requests small and fast


def polish_note(text):
    """Returns one of:
        {'status': 'OK', 'suggestion': str}
        {'status': 'NOT_CONFIGURED', 'message': str}
        {'status': 'FAIL', 'message': str}
    Never raises - callers must be able to let normal note-saving proceed
    regardless of what this returns.
    """
    text = (text or "").strip()
    if not text:
        return {"status": "FAIL", "message": "Nothing to rewrite."}
    if len(text) > MAX_INPUT_CHARS:
        return {"status": "FAIL", "message": f"Note is too long to polish (max {MAX_INPUT_CHARS} characters)."}

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return {"status": "NOT_CONFIGURED", "message": "AI rewriting is not configured."}

    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=600,
        )
        suggestion = (response.choices[0].message.content or "").strip()
        if not suggestion:
            return {"status": "FAIL", "message": "AI rewrite returned no text."}
        return {"status": "OK", "suggestion": suggestion}
    except Exception as exc:  # noqa: BLE001 - any provider/network failure must degrade gracefully
        return {"status": "FAIL", "message": f"AI rewrite failed: {exc}"}
