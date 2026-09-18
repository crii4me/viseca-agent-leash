"""
Function 1 of the Viseca "Agent on a Leash" control layer: the mandate compiler.

    from mandate_compiler import compile_mandate

    mandate = compile_mandate("Buy one ordinary grocery item for CHF 20 or less "
                              "from a shop I use regularly. Ask me when uncertain.")
    mandate.to_viseca_dict()

`compile_mandate` is pure: instruction text in, Mandate out. It is not wired to
the Viseca API - /healthz and /v1/bootstrap belong to Function 2's runtime, not
to mandate compilation.
"""

from .compile import BackendError, compile_mandate
from .config import Config
from .models import (
    FIELDS,
    Bucket,
    Clause,
    CompilerMeta,
    HardRule,
    Mandate,
    Operator,
    Scope,
    UncertaintyPolicy,
)
from .schema import mandate_json_schema, validation_errors, write_schema_file

__all__ = [
    "compile_mandate",
    "BackendError",
    "Config",
    "Mandate",
    "HardRule",
    "Clause",
    "CompilerMeta",
    "Bucket",
    "Operator",
    "Scope",
    "UncertaintyPolicy",
    "FIELDS",
    "mandate_json_schema",
    "validation_errors",
    "write_schema_file",
]

__version__ = "0.1.0"
