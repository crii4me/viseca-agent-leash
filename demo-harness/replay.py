"""
replay.py
=========
Offline replay harness -- the "test and simulation" path, no API key required.

For a scenario (or every scenario) it does the full round trip the challenge
is actually judged on:

    cardholder instruction
        -> Function 1 (mandate_compiler.compile_mandate)   [the mandate]
        -> build live-shaped events from the CSV data pack  [the purchases]
        -> Function 2 (decision_engine.decide), in replay_order, per card
        -> a table of approve / decline / step_up + the reason for each

State is maintained exactly as a correct live worker must (technical_details.md
step 8): only FINAL approvals go in the ledger (a step_up does not, until a
human resolves it -- which offline we don't simulate, so step_ups simply never
inflate a later window); repeated delivery of the same authorization_id is
recognised and not decided or counted twice; the simulated purchase timestamp
(not the wall clock) drives the rolling windows.

Usage:
    python replay.py                       # all scenarios, sample-data/
    python replay.py --scenario SCEN0001   # one scenario
    python replay.py --data-dir /path/to/viseca-2026/data
    python replay.py --json                # machine-readable, for the UI/harness

fx rates are loaded from the data pack's fx_rates.csv, so every currency in
the pack (CHF/EUR/GBP/USD) always converts -- the reason the decision ladder's
"currency unresolvable" branch never fires on real data.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# decision-engine and mandate-compiler live as sibling packages in this monorepo.
sys.path.insert(0, str(_HERE.parent / "decision-engine" / "src"))
sys.path.insert(0, str(_HERE.parent / "mandate-compiler" / "src"))

from build_events import DataPack  # noqa: E402
from decision_engine import Ledger, build_familiar, decide, decide_full  # noqa: E402
from mandate_compiler import compile_mandate  # noqa: E402


def load_fx_rates(data_dir: Path) -> dict[str, float]:
    """{currency -> rate that turns 1 unit of it into CHF}, from fx_rates.csv."""
    rates: dict[str, float] = {}
    with open(data_dir / "fx_rates.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["to_currency"] == "CHF":
                rates[row["from_currency"]] = float(row["rate"])
    return rates


def compile_scenario_mandate(pack: DataPack, scenario_id: str) -> dict:
    """Compile this scenario's cardholder instruction through Function 1."""
    instruction = pack.scenario_catalogue[scenario_id]["cardholder_instruction"]
    mandate = compile_mandate(instruction).to_viseca_dict()
    mandate["status"] = "active"  # a confirmed mandate; offline stubs need it set
    return mandate


def load_familiar_by_card(data_dir: Path) -> dict[str, list]:
    """Build each card's familiar-merchant list from authorization_history.csv,
    if present. Returns {} when the history file isn't bundled (the trimmed
    sample-data doesn't include it, so lookalike detection simply won't fire)."""
    hist_path = data_dir / "authorization_history.csv"
    if not hist_path.exists():
        return {}
    with open(hist_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    card_ids = {r.get("card_id") for r in rows if r.get("card_id")}
    return {cid: build_familiar(rows, cid) for cid in card_ids}


def replay_scenario(pack: DataPack, scenario_id: str, fx_rates: dict[str, float],
                    *, use_risk: bool = False, familiar_by_card: dict | None = None) -> list[dict]:
    """Replay one scenario in replay_order, returning a row per purchase.

    `use_risk=True` runs the full Function 2 (hard rules + risk composition);
    otherwise just the hard-rules engine, so you can diff the two."""
    mandate = compile_scenario_mandate(pack, scenario_id)
    events = pack.build_scenario_events(scenario_id, mandate=mandate)
    familiar_by_card = familiar_by_card or {}

    ledger = Ledger()
    rows: list[dict] = []
    for event in events:
        auth = event["authorization"]
        auth_id = auth["authorization_id"]

        # Idempotency: a redelivered live id is reconciled, not re-decided.
        if ledger.already_handled(auth_id):
            rows.append({
                "scenario_id": scenario_id, "authorization_id": auth_id,
                "source_id": auth["source_authorization_id"],
                "amount_chf": auth["billing_amount_chf"], "currency": auth["currency"],
                "decision": "(duplicate delivery — reconciled, not re-decided)",
                "reason_codes": ["duplicate_delivery"], "evidence": [],
            })
            continue
        ledger.record_seen(auth_id)

        if use_risk:
            familiar = familiar_by_card.get(auth["card_id"])
            d = decide_full(event, ledger=ledger, fx_rates=fx_rates, familiar=familiar)
        else:
            d = decide(event, ledger=ledger, fx_rates=fx_rates)

        # Only a FINAL approval goes in the ledger. A step_up does not (a human
        # hasn't confirmed it), so it can't inflate a later rolling window.
        if d.decision == "approve":
            ledger.record_approved(event)

        rows.append({
            "scenario_id": scenario_id, "authorization_id": auth_id,
            "source_id": auth["source_authorization_id"],
            "amount_chf": auth["billing_amount_chf"], "currency": auth["currency"],
            "decision": d.decision, "reason_codes": d.reason_codes,
            "evidence": d.evidence,
        })
    return rows


def _print_table(scenario_id: str, pack: DataPack, rows: list[dict]) -> None:
    cat = pack.scenario_catalogue[scenario_id]
    print(f"\n=== {scenario_id}: {cat['scenario_name']} ===")
    print(f"    instruction: {cat['cardholder_instruction']}")
    mandate = compile_scenario_mandate(pack, scenario_id)
    print(f"    compiled: {len(mandate['hard_rules'])} hard rule(s), "
          f"uncertainty_policy={mandate['uncertainty_policy']}, "
          f"{len(mandate['open_questions'])} open question(s)")
    print(f"    {'source':<8} {'amount':>12}  {'decision':<9} reason")
    print(f"    {'-'*8} {'-'*12}  {'-'*9} {'-'*40}")
    for r in rows:
        amt = f"{r['amount_chf']:.2f} CHF"
        reason = ", ".join(r["reason_codes"])
        print(f"    {r['source_id']:<8} {amt:>12}  {r['decision']:<9} {reason}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline replay of the Viseca data pack through Function 1 + Function 2.")
    ap.add_argument("--scenario", help="A single scenario id (e.g. SCEN0001). Default: all.")
    ap.add_argument("--data-dir", default=str(_HERE / "sample-data"),
                    help="Path to the data pack (viseca-2026/data). Defaults to the bundled sample-data/.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a table.")
    # Risk composition is ON by default. It used to be opt-in via --risk, which
    # meant a plain `python replay.py` silently ran only half of Function 2:
    # the duplicate-order and lookalike-seller cases in SCEN0004 both sailed
    # through, and the output looked like a finished, working system. The
    # partial engine is still reachable, but you now have to ask for it by name.
    ap.add_argument("--no-risk", action="store_true",
                    help="Run ONLY the hard-rules engine, skipping risk composition. "
                         "For diffing the two layers; not the real engine.")
    ap.add_argument("--risk", action="store_true",
                    help=argparse.SUPPRESS)  # accepted for backwards compatibility; now the default
    args = ap.parse_args()

    use_risk = not args.no_risk

    data_dir = Path(args.data_dir)
    pack = DataPack(data_dir)
    fx_rates = load_fx_rates(data_dir)
    familiar_by_card = load_familiar_by_card(data_dir) if use_risk else {}

    scenario_ids = [args.scenario] if args.scenario else sorted(pack.scenario_catalogue)

    all_rows: dict[str, list[dict]] = {}
    for scen in scenario_ids:
        all_rows[scen] = replay_scenario(pack, scen, fx_rates, use_risk=use_risk,
                                         familiar_by_card=familiar_by_card)

    if args.json:
        print(json.dumps(all_rows, indent=2))
        return

    for scen in scenario_ids:
        _print_table(scen, pack, all_rows[scen])

    if not use_risk:
        print("\n!! --no-risk: hard rules ONLY. Duplicate orders, lookalike sellers "
              "and order-term mismatches are NOT checked in this mode.")

    total = sum(len(r) for r in all_rows.values())
    counts: dict[str, int] = {}
    for rows in all_rows.values():
        for r in rows:
            key = r["decision"] if r["decision"] in ("approve", "decline", "step_up") else "other"
            counts[key] = counts.get(key, 0) + 1
    print(f"\n{total} purchases replayed: "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
