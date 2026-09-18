"""Regenerate the shared interface contract at /contracts/mandate.schema.json.

That file is the repo's single source of truth for the JSON that flows from
mandate-compiler into decision-engine. It is generated from the Pydantic models
rather than hand-maintained, so the published contract cannot drift from what
Function 1 actually emits.

Run this after any change to `models.HardRule` or the mandate shape, and commit
the result - decision-engine validates against the committed file.
"""

from pathlib import Path

from mandate_compiler import write_schema_file

if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parents[2]
    out = repo_root / "contracts" / "mandate.schema.json"
    print(f"wrote {write_schema_file(out)}")
