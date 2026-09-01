# Bettermind Labs Project

This repository contains the Bettermind Labs CT viewer and processing backend.

Contents
- `api/` — Flask API endpoints and processing modules
- `templates/` — HTML templates for the frontend viewer and pages
- `instance/` — sample instance data used for development and testing
- `marketing_media/`, `migrations/`, `tests/`, `tools/` — auxiliary files and utilities

Quick start

1. Create and activate a Python virtual environment (Python 3.8+ recommended):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run the app (example):

```bash
# set env vars as needed, then
FLASK_APP=manage.py FLASK_ENV=development flask run
```

Running tests

```bash
pip install -r tests/requirements-test.txt
pytest -q
```

Notes
- Adjust environment variables and configuration for production deployments.
- See `DATABASE_DESIGN.md` and `DICOM_PIPELINE.md` for architecture details.

License

Add a license file if needed. This project contains internal research code.
