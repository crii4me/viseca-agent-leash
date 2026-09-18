"""Compile every fixture instruction and print the result.

    python scripts/run_fixtures.py            # whatever MANDATE_BACKEND says
    python scripts/run_fixtures.py --json     # machine-readable, for diffing runs
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mandate_compiler import Config, compile_mandate

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "instructions.json"

BAR = "=" * 78


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--only", help="run a single fixture by id")
    args = ap.parse_args()

    cfg = Config.from_env()
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    if args.only:
        fixtures = [f for f in fixtures if f["id"] == args.only]

    if args.json:
        out = []
        for f in fixtures:
            m = compile_mandate(f["instruction"], config=cfg)
            out.append({"id": f["id"], "instruction": f["instruction"], "mandate": m.to_viseca_dict()})
        print(json.dumps(out, indent=2))
        return

    print(f"backend: {cfg.resolved_backend()}   model: {cfg.model}")

    for f in fixtures:
        m = compile_mandate(f["instruction"], config=cfg)
        d = m.to_viseca_dict()

        print(f"\n{BAR}\n{f['id']}\n{BAR}")
        print(f"  {f['why']}\n")
        print(f'  INSTRUCTION: "{f["instruction"]}"\n')

        print(f"  hard_rules ({len(d['hard_rules'])}):")
        if not d["hard_rules"]:
            print("    (none)")
        for r, rule in zip(d["hard_rules"], m.hard_rules):
            print(f"    - {rule.describe()}")
            print(f"      {json.dumps(r)}")

        print(f"\n  uncertainty_policy: {d['uncertainty_policy']}")

        print(f"\n  guidance ({len(d['guidance'])}):")
        for g in d["guidance"] or ["(none)"]:
            print(f"    - {g}")

        print(f"\n  open_questions ({len(d['open_questions'])}):")
        for q in d["open_questions"] or ["(none)"]:
            print(f"    - {q}")

        if m.meta and m.meta.warnings:
            print("\n  warnings:")
            for w in m.meta.warnings:
                print(f"    ! {w}")

        print(f"\n  [compiled in {m.meta.elapsed_ms:.2f} ms via {m.meta.backend}]")


if __name__ == "__main__":
    main()
