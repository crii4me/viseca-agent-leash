"""
JSON Schema for the canonical Viseca mandate, plus a validation gate.

Deliberate redundancy: the mandate is *produced* through Pydantic and *checked*
through `jsonschema`. Two different libraries, so a bug in Pydantic model
construction cannot wave itself through. Pydantic enforces the semantic
invariants (scope/field agreement, currency on money fields); jsonschema
enforces the structural contract that Function 2 - and any non-Python consumer -
can rely on.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from .models import HardRule, Mandate, UncertaintyPolicy


class VisecaMandate(BaseModel):
    """The four contract fields, and nothing else.

    Derived from the same `HardRule` model the compiler builds, so the published
    schema cannot drift from what we actually emit.
    """

    model_config = ConfigDict(extra="forbid")

    hard_rules: list[HardRule] = Field(default_factory=list)
    uncertainty_policy: UncertaintyPolicy = UncertaintyPolicy.ASK
    guidance: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


def mandate_json_schema() -> dict:
    """The JSON Schema for a compiled mandate."""
    schema = VisecaMandate.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "VisecaMandate"
    schema["description"] = (
        "Output contract of Function 1 (mandate compiler), input contract of "
        "Function 2 (decision engine). NOTE: a hard_rule's `value` is denominated "
        "in its `currency`, which is not necessarily CHF - convert via fx_rates "
        "before comparing, and fail closed if conversion is impossible."
    )
    return schema


_VALIDATOR = Draft202012Validator(mandate_json_schema())


def validation_errors(payload: dict) -> list[str]:
    """Structural errors in a mandate payload. Empty list means valid."""
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(_VALIDATOR.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]


def is_valid(payload: dict) -> bool:
    return not validation_errors(payload)


def assert_valid(mandate: Mandate) -> dict:
    """Validate a compiled mandate and return its canonical dict form.

    Raises ValueError with every structural problem listed, not just the first.
    """
    payload = mandate.to_viseca_dict()
    errors = validation_errors(payload)
    if errors:
        raise ValueError(
            "compiled mandate failed schema validation:\n  - " + "\n  - ".join(errors)
        )
    return payload


def write_schema_file(path: str | Path) -> Path:
    """Write the schema to disk so non-Python teammates can consume it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mandate_json_schema(), indent=2) + "\n", encoding="utf-8")
    return p
