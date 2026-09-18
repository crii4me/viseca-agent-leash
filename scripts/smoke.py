"""Quick end-to-end check against the worked example from Viseca's docs."""

import json

from mandate_compiler import Config, compile_mandate

WORKED_EXAMPLE = (
    "Buy one ordinary grocery item for CHF 20 or less from a shop I use regularly. "
    "Ask me when uncertain."
)

if __name__ == "__main__":
    cfg = Config.from_env()
    print(f"backend: {cfg.resolved_backend()}  model: {cfg.model}\n")

    m = compile_mandate(WORKED_EXAMPLE, config=cfg)

    print("--- canonical mandate ---")
    print(json.dumps(m.to_viseca_dict(), indent=2))

    print("\n--- clause routing ---")
    for c in m.clauses:
        print(f"  [{c.bucket.value:<18}] {c.text!r}")
        print(f"      why: {c.reason}")
        if c.rule:
            print(f"      rule: {c.rule.describe()}")

    if m.meta and m.meta.warnings:
        print("\n--- warnings ---")
        for w in m.meta.warnings:
            print(f"  ! {w}")
