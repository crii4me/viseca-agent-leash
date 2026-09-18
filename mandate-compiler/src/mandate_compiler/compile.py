"""
Function 1 entry point: `compile_mandate(instruction) -> Mandate`.

Pure by construction. The only input is the instruction string; the only output
is a Mandate. It reads no files, touches no ledger, and knows nothing about
authorizations, /healthz or /v1/bootstrap. The single side effect any backend may
have is one outbound model call, and the deterministic backend does not even do
that.

Assembly lives here rather than in the backends so that every backend - regex
today, a model tomorrow - is held to the same contract: rules are deduplicated,
unknown fields are demoted rather than enforced, and the result is validated
against the published JSON Schema before it is returned.
"""

from __future__ import annotations

import time

from .backends import Backend, BackendResult
from .backends.deterministic import DeterministicBackend
from .config import Config
from .models import (
    Bucket,
    Clause,
    CompilerMeta,
    HardRule,
    Mandate,
    UncertaintyPolicy,
)
from .schema import validation_errors


class BackendError(RuntimeError):
    """A backend could not produce an analysis."""


def _get_backend(config: Config) -> Backend:
    choice = config.resolved_backend()
    if choice == "anthropic":
        from .backends.anthropic_llm import AnthropicBackend

        return AnthropicBackend()
    return DeterministicBackend()


def _dedupe_rules(rules: list[HardRule]) -> list[HardRule]:
    """Drop exact duplicates while preserving first-seen order."""
    seen: set[tuple] = set()
    out: list[HardRule] = []
    for r in rules:
        value = tuple(r.value) if isinstance(r.value, list) else r.value
        key = (r.field, r.operator, value, r.currency, r.scope, r.period_days)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        s = (s or "").strip()
        if not s or s.casefold() in seen:
            continue
        seen.add(s.casefold())
        out.append(s)
    return out


def _assemble(
    instruction: str,
    result: BackendResult,
    config: Config,
    backend_name: str,
    elapsed_ms: float,
) -> Mandate:
    hard_rules = [c.rule for c in result.clauses if c.bucket is Bucket.HARD_RULE and c.rule]
    guidance = [c.guidance for c in result.clauses if c.bucket is Bucket.GUIDANCE and c.guidance]
    questions = [c.question for c in result.clauses if c.bucket is Bucket.OPEN_QUESTION and c.question]
    clauses = list(result.clauses)
    warnings = list(result.warnings)

    policy = result.uncertainty_policy
    if policy is None:
        # The customer did not say. `ask` is the control-preserving default: it
        # hands the decision back to them instead of the system choosing for
        # them. We record that we defaulted rather than letting it pass silently.
        policy = UncertaintyPolicy.ASK
        questions.append(
            "Your instruction doesn't say what should happen when the agent isn't "
            "sure whether a purchase is allowed. It has been set to ask you each "
            "time - would you rather it declined automatically, or approved?"
        )
        clauses.append(
            Clause(
                text="",
                bucket=Bucket.OPEN_QUESTION,
                reason="No uncertainty behaviour stated; defaulted to 'ask' and flagged it.",
                question=questions[-1],
                confidence=1.0,
            )
        )
        warnings.append("uncertainty_policy defaulted to 'ask' (not stated in the instruction)")

    mandate = Mandate(
        hard_rules=_dedupe_rules(hard_rules),
        uncertainty_policy=policy,
        guidance=_dedupe_strings(guidance),
        open_questions=_dedupe_strings(questions),
        source_instruction=instruction,
        clauses=clauses,
        meta=CompilerMeta(
            backend=backend_name,
            model=result.model,
            elapsed_ms=round(elapsed_ms, 2),
            warnings=warnings,
        ),
    )

    provisional = mandate.provisional_fields_used
    if provisional and mandate.meta:
        mandate.meta.warnings.append(
            "hard rules use fields not yet confirmed against Viseca's event schema: "
            + ", ".join(provisional)
        )

    errors = validation_errors(mandate.to_viseca_dict())
    if errors:
        raise ValueError(
            "compiled mandate failed schema validation:\n  - " + "\n  - ".join(errors)
        )

    return mandate


def compile_mandate(
    instruction: str,
    *,
    config: Config | None = None,
    backend: Backend | None = None,
) -> Mandate:
    """Compile a plain-English spending instruction into a structured mandate.

    Args:
        instruction: The customer's instruction, exactly as they wrote it.
        config: Optional settings; defaults to `Config.from_env()`.
        backend: Optional explicit backend, mainly for tests.

    Returns:
        A `Mandate` that has passed JSON Schema validation.

    Raises:
        ValueError: if `instruction` is empty, or the result fails validation.
        BackendError: if an explicitly-selected backend fails.
    """
    if instruction is None or not instruction.strip():
        raise ValueError("instruction must be a non-empty string")

    config = config or Config.from_env()
    chosen = backend or _get_backend(config)

    started = time.perf_counter()
    try:
        result = chosen.analyze(instruction, config)
        name = chosen.name
    except Exception as exc:
        # `auto` means "use the model if you can, but never fail closed to
        # nothing" - this is the deterministic fallback the brief asks for.
        if backend is None and config.backend == "auto" and chosen.name != "deterministic":
            fallback = DeterministicBackend()
            result = fallback.analyze(instruction, config)
            name = fallback.name
            result.warnings.append(f"{chosen.name} backend failed ({exc}); used deterministic fallback")
        else:
            raise BackendError(f"{chosen.name} backend failed: {exc}") from exc

    elapsed_ms = (time.perf_counter() - started) * 1000
    return _assemble(instruction, result, config, name, elapsed_ms)
