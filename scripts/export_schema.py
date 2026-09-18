"""Write the mandate JSON Schema to schema/mandate.schema.json.

Function 2 (and anything not written in Python) should validate against this file
rather than importing the Pydantic models.
"""

from pathlib import Path

from mandate_compiler import write_schema_file

if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "schema" / "mandate.schema.json"
    print(f"wrote {write_schema_file(out)}")
