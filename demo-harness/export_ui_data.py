"""
export_ui_data.py
=================
Generate the frontend's data file from the REAL engine.

The demo UI used to ship `mockDecisionData.js`: three invented purchases at
merchants that do not exist in Viseca's data, judged by a second decision engine
hand-written in JavaScript. Nothing it displayed came from the system actually
being demoed.

This replaces that. Every field below is produced by running the real
Function 1 (mandate_compiler) and the real Function 2 (decision_engine, with
risk composition) over the real Viseca data pack. Decision latency is MEASURED,
not simulated - it is the wall-clock cost of the decide_full() call.

    python export_ui_data.py --data-dir ../vendor/viseca-2026/data

Writes frontend/src/realDecisionData.js. Re-run it whenever the engine changes;
the file is generated and should never be hand-edited.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from replay import (  # noqa: E402  (replay sets up the sibling package paths)
    DataPack,
    compile_scenario_mandate,
    load_familiar_by_card,
    load_fx_rates,
)
from decision_engine import Ledger, decide_full  # noqa: E402
from mandate_compiler import compile_mandate  # noqa: E402

_HERE = Path(__file__).resolve().parent
DEFAULT_OUT = _HERE.parent / "frontend" / "src" / "realDecisionData.js"

# The live platform's deadline, from technical_details.md. The UI shows measured
# latency against this, so the SLA badge means something real.
DEADLINE_MS = 8000


def _first_item(auth: dict) -> dict:
    items = auth.get("items") or []
    return items[0] if items else {}


def _measure(event, ledger, fx_rates, familiar, repeats: int = 5):
    """Decide once for the result, then time repeats for a stable latency.

    The first call carries import/JIT warmth that is not representative, so the
    reported number is the median of `repeats` timed runs against a throwaway
    ledger - the real cost of evaluating this purchase, not of the first one.
    """
    decision = decide_full(event, ledger=ledger, fx_rates=fx_rates, familiar=familiar)

    samples = []
    for _ in range(repeats):
        throwaway = Ledger()
        for prior in ledger.approved_events() if hasattr(ledger, "approved_events") else []:
            throwaway.record_approved(prior)
        t0 = time.perf_counter()
        decide_full(event, ledger=throwaway, fx_rates=fx_rates, familiar=familiar)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return decision, round(samples[len(samples) // 2], 3)


def build(data_dir: Path) -> dict:
    pack = DataPack(data_dir)
    fx_rates = load_fx_rates(data_dir)
    familiar_by_card = load_familiar_by_card(data_dir)

    scenarios = []
    totals = {"approve": 0, "decline": 0, "step_up": 0}

    for scenario_id in sorted(pack.scenario_catalogue):
        cat = pack.scenario_catalogue[scenario_id]
        instruction = cat["cardholder_instruction"]

        mandate = compile_scenario_mandate(pack, scenario_id)
        compiled = compile_mandate(instruction)  # for rule_summaries / meta

        events = pack.build_scenario_events(scenario_id, mandate=mandate)
        ledger = Ledger()
        purchases = []

        for event in events:
            auth = event["authorization"]
            auth_id = auth["authorization_id"]
            if ledger.already_handled(auth_id):
                continue
            ledger.record_seen(auth_id)

            familiar = familiar_by_card.get(auth["card_id"])
            decision, latency_ms = _measure(event, ledger, fx_rates, familiar)

            if decision.decision == "approve":
                ledger.record_approved(event)

            item = _first_item(auth)
            merchant = auth.get("merchant") or {}
            fam_list = familiar or []
            approved_count = next(
                (f.approved_count for f in fam_list
                 if getattr(f, "merchant_id", None) == merchant.get("merchant_id")),
                0,
            )

            if decision.decision in totals:
                totals[decision.decision] += 1

            purchases.append({
                "id": auth.get("source_authorization_id") or auth_id,
                "authorizationId": auth_id,
                "product": item.get("item_name", ""),
                "itemDetails": item.get("item_details", ""),
                "itemCategory": item.get("item_category", ""),
                "merchant": merchant.get("merchant_name", ""),
                "merchantCategory": merchant.get("merchant_category", ""),
                "merchantCountry": merchant.get("merchant_country", ""),
                "merchantApprovedCount": approved_count,
                "amount": auth.get("amount"),
                "currency": auth.get("currency"),
                "amountChf": auth.get("billing_amount_chf"),
                "orderReturnable": auth.get("order_returnable", "unknown"),
                "itemCount": len(auth.get("items") or []),
                "decision": decision.decision,
                "reasonCodes": list(decision.reason_codes),
                "customerMessage": decision.customer_message,
                "evidence": list(decision.evidence),
                "latencyMs": latency_ms,
            })

        scenarios.append({
            "id": scenario_id,
            "name": cat.get("scenario_name", scenario_id),
            "controlQuestion": cat.get("control_question", ""),
            "instruction": instruction,
            "mandate": {
                "hardRules": mandate.get("hard_rules", []),
                "ruleSummaries": compiled.rule_summaries,
                "uncertaintyPolicy": mandate.get("uncertainty_policy", "ask"),
                "guidance": mandate.get("guidance", []),
                "openQuestions": mandate.get("open_questions", []),
                "blockingOpenQuestions": mandate.get("blocking_open_questions", []),
            },
            "purchases": purchases,
        })

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataPack": str(data_dir),
        "deadlineMs": DEADLINE_MS,
        "totals": totals,
        "scenarios": scenarios,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=str(_HERE / "sample-data"))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--sample", action="store_true",
                    help="Print one decision to stdout instead of writing the file.")
    args = ap.parse_args()

    payload = build(Path(args.data_dir))

    if args.sample:
        s = payload["scenarios"][0]
        print(json.dumps({**s, "purchases": s["purchases"][:1]}, indent=2)[:3000])
        return

    body = json.dumps(payload, indent=2, ensure_ascii=False)
    js = (
        "// GENERATED FILE - do not edit by hand.\n"
        "// Produced by demo-harness/export_ui_data.py from the real engine:\n"
        "//   Function 1 (mandate_compiler) + Function 2 (decision_engine, with\n"
        "//   risk composition), run over Viseca's own data pack.\n"
        "// `latencyMs` on each purchase is MEASURED, not simulated.\n"
        f"// Generated: {payload['generatedAt']}\n\n"
        f"const data = {body}\n\n"
        "export default data\n"
        "export const { scenarios, totals, deadlineMs, generatedAt } = data\n"
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(js, encoding="utf-8")

    n = sum(len(s["purchases"]) for s in payload["scenarios"])
    print(f"wrote {out}")
    print(f"  {len(payload['scenarios'])} scenarios, {n} purchases")
    print(f"  totals: {payload['totals']}")


if __name__ == "__main__":
    main()
