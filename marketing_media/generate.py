"""
CLI for the public marketing media pipeline.

    python marketing_media/generate.py list
    python marketing_media/generate.py validate
    python marketing_media/generate.py dry-run <prompt_id>
    python marketing_media/generate.py manifest

This is an offline build step run by a maintainer. The website never invokes
it. Generation defaults to a dry run so validation can be exercised without
spending API credit; see higgsfield/client.submit_generation.

Higgsfield never receives patient data - see higgsfield/guard.py.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from marketing_media.higgsfield.client import (  # noqa: E402
    build_request, submit_generation, load_manifest,
)
from marketing_media.higgsfield.guard import (  # noqa: E402
    load_approved_prompts, validate_approved_prompt, HiggsfieldPrivacyError,
)


def cmd_list():
    prompts = load_approved_prompts()
    print(f"{len(prompts)} approved prompt(s):\n")
    for pid, entry in prompts.items():
        print(f"  {pid}")
        print(f"    section    : {entry.get('section')}")
        print(f"    media_type : {entry.get('media_type')}")
        print(f"    review     : {entry.get('review_status')}")
        print()


def cmd_validate():
    prompts = load_approved_prompts()
    failures = []
    for pid in prompts:
        try:
            validate_approved_prompt(pid, prompts=prompts)
            print(f"OK    {pid}")
        except HiggsfieldPrivacyError as exc:
            failures.append((pid, str(exc)))
            print(f"FAIL  {pid}: {exc}")
    if failures:
        print(f"\n{len(failures)} prompt(s) failed validation.")
        sys.exit(1)
    print(f"\nAll {len(prompts)} prompts passed the privacy/safety checks.")


def cmd_dry_run(prompt_id):
    result = submit_generation(prompt_id, dry_run=True)
    print(json.dumps(result, indent=2))


def cmd_manifest():
    manifest = load_manifest()
    assets = manifest.get("assets", [])
    if not assets:
        print("No reviewed assets in the manifest.")
        print("The public website is using its built-in procedural visuals.")
        return
    for a in assets:
        print(f"{a['prompt_id']:<28} {a['media_type']:<6} {a['asset_path']}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "list":
        cmd_list()
    elif cmd == "validate":
        cmd_validate()
    elif cmd == "dry-run":
        if len(sys.argv) < 3:
            print("usage: generate.py dry-run <prompt_id>")
            sys.exit(1)
        cmd_dry_run(sys.argv[2])
    elif cmd == "manifest":
        cmd_manifest()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
