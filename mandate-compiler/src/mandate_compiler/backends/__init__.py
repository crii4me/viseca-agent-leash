"""
Backend interface.

A backend's only job is: instruction text -> list of classified `Clause`s.
Assembly into a `Mandate`, validation, and the unknown-field demotion all happen
once, in `compile.py`, so every backend is held to the same standard. In
particular this is what guarantees that a model-produced rule cannot name a
field the decision engine has no way to evaluate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from ..config import Config
from ..models import Clause, HardRule, UncertaintyPolicy


@dataclass
class BackendResult:
    """What a backend hands back to the assembler."""

    clauses: list[Clause] = field(default_factory=list)
    uncertainty_policy: UncertaintyPolicy | None = None
    warnings: list[str] = field(default_factory=list)
    model: str | None = None


@runtime_checkable
class Backend(Protocol):
    name: str

    def analyze(self, instruction: str, config: Config) -> BackendResult: ...


def safe_rule(**kwargs) -> tuple[HardRule | None, str | None]:
    """Build a HardRule, returning (rule, None) or (None, why_it_failed).

    Never raises. A rule that fails validation - unknown field, scope mismatch,
    missing currency - is a rule we must not enforce, so the caller demotes it
    rather than the process dying. This is the single chokepoint through which
    every candidate rule, from every backend, has to pass.
    """
    try:
        return HardRule(**kwargs), None
    except ValidationError as exc:
        # Pull the actual messages out. `str(exc)` ends with Pydantic's docs URL,
        # so taking the last line yields boilerplate instead of the reason - and
        # these strings end up in the warnings and explanations a judge reads.
        parts: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msg = str(err.get("msg", "")).removeprefix("Value error, ")
            parts.append(f"{loc}: {msg}" if loc else msg)
        return None, "; ".join(p for p in parts if p) or str(exc)
    except Exception as exc:
        return None, str(exc)
