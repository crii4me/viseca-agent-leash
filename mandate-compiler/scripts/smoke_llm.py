"""
First live check of the Anthropic backend. Run this once a key exists.

    set ANTHROPIC_API_KEY=sk-...
    python scripts/smoke_llm.py

Until it has been run at least once, treat the anthropic backend as unverified:
everything in it except the network call is covered by tests/test_llm_backend.py,
but the request shape itself has never been sent.
"""

from __future__ import annotations

import json
import sys

from mandate_compiler import Config, compile_mandate
from mandate_compiler.backends.anthropic_llm import AnthropicBackend

INSTRUCTION = (
    "Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. "
    "Ask me when uncertain."
)

if __name__ == "__main__":
    cfg = Config.from_env()
    if not cfg.api_key:
        print("ANTHROPIC_API_KEY is not set - nothing to smoke test.", file=sys.stderr)
        raise SystemExit(1)

    print(f"model: {cfg.model}\ninstruction: {INSTRUCTION}\n")

    mandate = compile_mandate(INSTRUCTION, config=cfg, backend=AnthropicBackend())

    print(json.dumps(mandate.to_viseca_dict(), indent=2))
    print(f"\n[{mandate.meta.elapsed_ms:.0f} ms via {mandate.meta.backend}]")

    if mandate.meta.warnings:
        print("\nwarnings:")
        for w in mandate.meta.warnings:
            print(f"  ! {w}")

    print("\n--- clause routing ---")
    for c in mandate.clauses:
        print(f"  [{c.bucket.value:<18}] {c.text!r}\n      {c.reason}")
