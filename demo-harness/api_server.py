"""
api_server.py
=============
A tiny local API so the demo UI can run Function 1 and Function 2 LIVE.

Optional. The frontend ships a generated data file (export_ui_data.py) and works
with zero setup; this server is what lets you type a fresh instruction in front
of judges and watch the real compiler turn it into rules, then apply those rules
to a real Viseca purchase.

Standard library only - no FastAPI, no Flask, nothing to install.

    python api_server.py --data-dir ../vendor/viseca-2026/data
    -> http://127.0.0.1:8787

Endpoints
    GET  /api/health
         {"ok": true, "scenarios": 5, "purchases": 45}

    POST /api/compile           {"instruction": "..."}
         Function 1 only. Returns the compiled mandate, the plain-English rule
         summaries, and how long compilation took.

    POST /api/decide            {"instruction": "...", "purchaseId": "AU0001"}
         The full round trip: compile the instruction, then run Function 2 over
         that real purchase under the freshly compiled mandate. This is the one
         worth demoing - it proves the customer's own words drive the outcome.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from replay import (  # noqa: E402
    DataPack,
    load_familiar_by_card,
    load_fx_rates,
)
from decision_engine import Ledger, decide_full  # noqa: E402
from mandate_compiler import compile_mandate  # noqa: E402

_HERE = Path(__file__).resolve().parent

STATE: dict = {}


def _load(data_dir: Path) -> None:
    pack = DataPack(data_dir)
    STATE["pack"] = pack
    STATE["fx"] = load_fx_rates(data_dir)
    STATE["familiar"] = load_familiar_by_card(data_dir)

    # Index every purchase by its source id (AU0001 ...) so the UI can ask for
    # one by name. Events are rebuilt per request against the live mandate, so
    # only the scenario + index is cached here.
    index: dict[str, tuple[str, int]] = {}
    for scenario_id in sorted(pack.scenario_catalogue):
        rows = pack.purchase_attempts_for(scenario_id) if hasattr(
            pack, "purchase_attempts_for") else None
        events = pack.build_scenario_events(scenario_id, mandate=_stub_mandate())
        for i, ev in enumerate(events):
            src = ev["authorization"].get("source_authorization_id")
            if src:
                index[src] = (scenario_id, i)
    STATE["index"] = index


def _stub_mandate() -> dict:
    """A placeholder mandate for event construction; the real one is swapped in
    per request. build_scenario_events only embeds it, it does not evaluate."""
    return {
        "hard_rules": [], "uncertainty_policy": "ask", "guidance": [],
        "open_questions": [], "blocking_open_questions": [], "status": "active",
    }


def compile_live(instruction: str) -> dict:
    t0 = time.perf_counter()
    mandate = compile_mandate(instruction)
    ms = (time.perf_counter() - t0) * 1000.0
    d = mandate.to_viseca_dict()
    return {
        "instruction": instruction,
        "hardRules": d["hard_rules"],
        "ruleSummaries": mandate.rule_summaries,
        "uncertaintyPolicy": d["uncertainty_policy"],
        "guidance": d["guidance"],
        "openQuestions": d["open_questions"],
        "blockingOpenQuestions": d["blocking_open_questions"],
        "compileMs": round(ms, 3),
        "warnings": list(mandate.meta.warnings) if mandate.meta else [],
    }


def decide_live(instruction: str, purchase_id: str) -> dict:
    pack: DataPack = STATE["pack"]
    index = STATE["index"]
    if purchase_id not in index:
        raise KeyError(purchase_id)

    scenario_id, position = index[purchase_id]

    compiled = compile_mandate(instruction)
    mandate = compiled.to_viseca_dict()
    mandate["status"] = "active"

    events = pack.build_scenario_events(scenario_id, mandate=mandate)
    event = events[position]
    auth = event["authorization"]

    familiar = STATE["familiar"].get(auth["card_id"])
    ledger = Ledger()

    t0 = time.perf_counter()
    decision = decide_full(event, ledger=ledger, fx_rates=STATE["fx"], familiar=familiar)
    ms = (time.perf_counter() - t0) * 1000.0

    item = (auth.get("items") or [{}])[0]
    merchant = auth.get("merchant") or {}
    return {
        "purchaseId": purchase_id,
        "scenarioId": scenario_id,
        "mandate": compile_live(instruction),
        "purchase": {
            "product": item.get("item_name", ""),
            "itemDetails": item.get("item_details", ""),
            "merchant": merchant.get("merchant_name", ""),
            "merchantCategory": merchant.get("merchant_category", ""),
            "amount": auth.get("amount"),
            "currency": auth.get("currency"),
            "amountChf": auth.get("billing_amount_chf"),
            "orderReturnable": auth.get("order_returnable", "unknown"),
        },
        "decision": decision.decision,
        "reasonCodes": list(decision.reason_codes),
        "customerMessage": decision.customer_message,
        "evidence": list(decision.evidence),
        "latencyMs": round(ms, 3),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console during a demo
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The Vite dev server is a different origin; this API is local-only.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, {})

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/api/health":
            n = sum(len(STATE["pack"].build_scenario_events(s, mandate=_stub_mandate()))
                    for s in STATE["pack"].scenario_catalogue)
            return self._send(200, {
                "ok": True,
                "scenarios": len(STATE["pack"].scenario_catalogue),
                "purchases": n,
            })
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON body"})

        path = self.path.rstrip("/")
        instruction = (payload.get("instruction") or "").strip()

        try:
            if path == "/api/compile":
                if not instruction:
                    return self._send(400, {"error": "instruction is required"})
                return self._send(200, compile_live(instruction))

            if path == "/api/decide":
                if not instruction:
                    return self._send(400, {"error": "instruction is required"})
                pid = payload.get("purchaseId") or "AU0001"
                return self._send(200, decide_live(instruction, pid))
        except KeyError as exc:
            return self._send(404, {"error": f"unknown purchase {exc}"})
        except Exception as exc:  # never crash the demo server
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        self._send(404, {"error": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=str(_HERE / "sample-data"))
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    _load(Path(args.data_dir))
    n = len(STATE["index"])
    print(f"data pack : {args.data_dir}")
    print(f"indexed   : {n} purchases across {len(STATE['pack'].scenario_catalogue)} scenarios")
    print(f"listening : http://127.0.0.1:{args.port}")
    print("            GET  /api/health")
    print("            POST /api/compile  {instruction}")
    print("            POST /api/decide   {instruction, purchaseId}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
