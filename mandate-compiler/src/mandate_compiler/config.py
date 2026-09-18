"""Env-based configuration. Nothing here reaches the network."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

BackendName = Literal["deterministic", "anthropic", "auto"]
UnresolvableTo = Literal["guidance", "open_question", "both"]

DEFAULT_MODEL = "claude-opus-5"


@dataclass(frozen=True)
class Config:
    """Compiler settings.

    `backend="auto"` picks the Anthropic backend when a key is present and falls
    back to the deterministic one otherwise. That is the sane default for a team
    where only some machines have a key, and it means the test suite runs
    identically on a laptop with no credentials.
    """

    backend: BackendName = "auto"
    model: str = DEFAULT_MODEL
    api_key: str | None = None
    max_tokens: int = 8000

    # How to route a clause that shapes judgment but cannot be resolved from the
    # instruction alone (the "a shop I use regularly" case). Default is `both`:
    # Function 2 still gets something to reason with, AND the customer gets asked.
    unresolvable_to: UnresolvableTo = "both"

    # If True, a hard rule naming a field outside the known vocabulary is demoted
    # to guidance + an open question instead of raising. Keep this True: it is the
    # guard that stops a hallucinated or injected field becoming an enforced rule.
    demote_unknown_fields: bool = True

    @classmethod
    def from_env(cls) -> Config:
        backend = os.getenv("MANDATE_BACKEND", "auto").strip().lower()
        if backend not in ("deterministic", "anthropic", "auto"):
            raise ValueError(
                f"MANDATE_BACKEND must be deterministic|anthropic|auto, got {backend!r}"
            )

        unresolvable = os.getenv("MANDATE_UNRESOLVABLE_TO", "both").strip().lower()
        if unresolvable not in ("guidance", "open_question", "both"):
            raise ValueError(
                "MANDATE_UNRESOLVABLE_TO must be guidance|open_question|both, "
                f"got {unresolvable!r}"
            )

        key = os.getenv("ANTHROPIC_API_KEY") or None

        return cls(
            backend=backend,  # type: ignore[arg-type]
            model=os.getenv("MANDATE_MODEL", DEFAULT_MODEL).strip(),
            api_key=key,
            max_tokens=int(os.getenv("MANDATE_MAX_TOKENS", "8000")),
            unresolvable_to=unresolvable,  # type: ignore[arg-type]
            demote_unknown_fields=os.getenv("MANDATE_DEMOTE_UNKNOWN_FIELDS", "1") != "0",
        )

    def resolved_backend(self) -> Literal["deterministic", "anthropic"]:
        """Turn `auto` into a concrete choice based on whether a key exists."""
        if self.backend == "auto":
            return "anthropic" if self.api_key else "deterministic"
        return self.backend  # type: ignore[return-value]
