"""
build_events.py
================
Turns the "Agent on a Leash" CSV data pack into live-shaped
`authorization.request` events, exactly as described in
technical_details.md section 3 ("Test your engine offline").

This is a *data builder*, not a decision engine. It:
  - joins purchase_attempts.csv + purchase_attempt_items.csv + merchants.csv
    + scenario_authorities.csv + scenario_catalogue.csv,
  - converts every value to the type the live event schema requires
    (numbers stay numbers, empty optional fields become null, etc.),
  - fills in a synthetic `mandate` / `context` / `runtime` block so the
    result validates against schemas/authorization_event.schema.json,
  - never invents facts: every field comes straight from the CSV row it
    corresponds to.

Usage:
    from build_events import DataPack
    pack = DataPack("data")
    events = pack.build_scenario_events("SCEN0001", mandate=my_mandate_stub)
    for event in events:
        decision = my_engine.decide(event)
"""

from __future__ import annotations

import copy
import csv
import datetime as dt
import decimal
import json
from pathlib import Path
from typing import Any, Iterable

DECIMAL_2 = decimal.Decimal("0.01")


def chf_round(value: decimal.Decimal) -> float:
    """Two-decimal, half-even rounding, as required by data_dictionary.md."""
    return float(value.quantize(DECIMAL_2, rounding=decimal.ROUND_HALF_EVEN))


def _to_number(value: str) -> float:
    return float(value)


def _to_int(value: str) -> int:
    return int(value)


def _null_if_empty(value: str | None):
    return value if value not in (None, "") else None


class DataPack:
    """Loads every CSV once and exposes joined, typed lookups."""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.merchants = self._index("merchants.csv", "merchant_id")
        self.customers = self._index("customers.csv", "customer_id")
        self.accounts = self._rows("accounts.csv")
        self.cards = self._index("cards.csv", "card_id")
        self.items_catalogue = self._index("items.csv", "item_id")
        self.fx_rates = {r["from_currency"]: decimal.Decimal(r["rate"]) for r in self._rows("fx_rates.csv")}
        self.scenario_catalogue = self._index("scenario_catalogue.csv", "scenario_id")
        self.scenario_authorities = self._index("scenario_authorities.csv", "authority_id")
        self.purchase_attempts = self._rows("purchase_attempts.csv")
        self.purchase_attempt_items = self._rows("purchase_attempt_items.csv")

        self._items_by_auth: dict[str, list[dict]] = {}
        for row in self.purchase_attempt_items:
            self._items_by_auth.setdefault(row["authorization_id"], []).append(row)
        for auth_id in self._items_by_auth:
            self._items_by_auth[auth_id].sort(key=lambda r: int(r["line_no"]))

        self._attempts_by_id = {r["authorization_id"]: r for r in self.purchase_attempts}
        self._attempts_by_scenario: dict[str, list[dict]] = {}
        for row in self.purchase_attempts:
            self._attempts_by_scenario.setdefault(row["scenario_id"], []).append(row)
        for scen in self._attempts_by_scenario:
            self._attempts_by_scenario[scen].sort(key=lambda r: int(r["replay_order"]))

    # -- csv helpers ---------------------------------------------------
    def _rows(self, name: str) -> list[dict]:
        with open(self.dir / name, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _index(self, name: str, key: str) -> dict[str, dict]:
        return {r[key]: r for r in self._rows(name)}

    # -- money -----------------------------------------------------------
    def to_chf(self, amount: str | float, currency: str) -> float:
        amt = decimal.Decimal(str(amount))
        rate = self.fx_rates[currency]
        return chf_round(amt * rate)

    # -- building blocks ---------------------------------------------------
    def _build_merchant(self, merchant_id: str) -> dict:
        m = self.merchants[merchant_id]
        return {
            "merchant_id": m["merchant_id"],
            "merchant_name": m["merchant_name"],
            "merchant_category": m["merchant_category"],
            "merchant_mcc": m["merchant_mcc"],
            "merchant_country": m["merchant_country"],
            "merchant_city": m["merchant_city"],
            "availability": m["availability"],
            "recurring_capable": m["recurring_capable"],
        }

    def _build_items(self, authorization_id: str) -> list[dict]:
        out = []
        for row in self._items_by_auth.get(authorization_id, []):
            out.append(
                {
                    "line_no": _to_int(row["line_no"]),
                    "item_id": row["item_id"],
                    "item_name": row["item_name"],
                    "item_category": row["item_category"],
                    "quantity": _to_int(row["quantity"]),
                    "unit_price": _to_number(row["unit_price"]),
                    "currency": row["currency"],
                    "item_details": row["item_details"],
                }
            )
        if not out:
            raise ValueError(f"{authorization_id} has no cart lines in purchase_attempt_items.csv")
        return out

    def build_authorization(self, authorization_id: str, *, live_authorization_id: str | None = None,
                             related_id_map: dict[str, str] | None = None) -> dict:
        """Builds the `authorization` object for one purchase_attempts.csv row.

        `live_authorization_id` lets you simulate the platform assigning a
        fresh per-run ID while keeping `source_authorization_id` pointing at
        the CSV row, exactly as technical_details.md describes.
        `related_id_map` lets you rewrite `related_authorization_id` the same
        way the live API does (source AU id -> the run's live id).
        """
        row = self._attempts_by_id[authorization_id]
        related_source = _null_if_empty(row["related_authorization_id"])
        related_live = None
        if related_source is not None:
            if related_id_map and related_source in related_id_map:
                related_live = related_id_map[related_source]
            else:
                related_live = related_source

        return {
            "authorization_id": live_authorization_id or row["authorization_id"],
            "source_authorization_id": row["authorization_id"],
            "scenario_id": row["scenario_id"],
            "replay_order": _to_int(row["replay_order"]),
            "mandate_id": "TM_OFFLINE_TEST",
            "profile_id": row["authority_id"],
            "card_id": row["card_id"],
            "initiator_type": "agent",
            "merchant": self._build_merchant(row["merchant_id"]),
            "timestamp": row["timestamp"],
            "amount": _to_number(row["amount"]),
            "currency": row["currency"],
            "billing_amount_chf": _to_number(row["billing_amount_chf"]),
            "items_subtotal": _to_number(row["items_subtotal"]),
            "delivery_fee": _to_number(row["delivery_fee"]),
            "channel": row["channel"],
            "customer_device_id": row["customer_device_id"],
            "authority_status": row["authority_status"],
            "card_status_at_attempt": row["card_status_at_attempt"],
            "spend_in_period_before_chf": _null_if_empty(row["spend_in_period_before_chf"]) and _to_number(row["spend_in_period_before_chf"]),
            "recent_attempt_count_10m": _to_int(row["recent_attempt_count_10m"]),
            "fulfillment_method": row["fulfillment_method"],
            "delivery_by": _null_if_empty(row["delivery_by"]),
            "order_returnable": row["order_returnable"],
            "order_cancellable": row["order_cancellable"],
            "related_authorization_id": related_live,
            "related_authorization_status": _null_if_empty(row["related_authorization_status"]),
            "purchase_description": row["purchase_description"],
            "items": self._build_items(authorization_id),
        }

    @staticmethod
    def mandate_stub(instruction: str, hard_rules: list[dict] | None = None,
                      uncertainty_policy: str = "ask", customer_id: str = "CU_OFFLINE",
                      card_id: str = "CA_OFFLINE", profile_id: str = "AUTH_OFFLINE") -> dict:
        return {
            "mandate_id": "TM_OFFLINE_TEST",
            "status": "active",
            "customer_id": customer_id,
            "card_id": card_id,
            "instruction": instruction,
            "hard_rules": hard_rules or [],
            "uncertainty_policy": uncertainty_policy,
            "profile_id": profile_id,
        }

    @staticmethod
    def _runtime(received_at: str) -> dict:
        return {
            "received_at": received_at,
            "history_window_minutes": 10,
            "context_basis": "run_decisions_and_scenario_timestamps",
        }

    def build_scenario_events(self, scenario_id: str, mandate: dict | None = None,
                               deadline_seconds: int = 8) -> list[dict]:
        """Replays a whole scenario in replay_order, as a list of complete
        events (type/request_id/deadline_at/authorization/mandate/context/runtime).

        `context.recent_authorizations` / `approved_spend_in_period_chf` are
        left empty: technical_details.md is explicit that this state is
        computed from *your own* run decisions, not supplied by the data pack.
        Your test harness (see tests/) is expected to fill these in as it
        replays a scenario against your own engine.
        """
        rows = self._attempts_by_scenario.get(scenario_id)
        if not rows:
            raise KeyError(f"Unknown scenario_id {scenario_id!r}")

        scen = self.scenario_catalogue[scenario_id]
        auth = self.scenario_authorities[rows[0]["authority_id"]]
        mandate = mandate or self.mandate_stub(
            scen["cardholder_instruction"],
            customer_id=auth["customer_id"],
            card_id=auth["card_id"],
            profile_id=rows[0]["authority_id"],
        )

        related_id_map: dict[str, str] = {}
        events = []
        for i, row in enumerate(rows, start=1):
            live_id = f"LIVE_{row['authorization_id']}"
            related_id_map[row["authorization_id"]] = live_id
            authorization = self.build_authorization(
                row["authorization_id"], live_authorization_id=live_id, related_id_map=related_id_map
            )
            occurred_at = row["timestamp"]
            received_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            deadline_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=deadline_seconds)
            deadline_at = deadline_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

            event = {
                "type": "authorization.request",
                "request_id": f"req_offline_{row['authorization_id']}",
                "deadline_at": deadline_at,
                "authorization": authorization,
                "mandate": copy.deepcopy(mandate),
                "context": {
                    "approved_spend_in_period_chf": None,
                    "recent_authorizations": [],
                },
                "runtime": self._runtime(received_at),
            }
            events.append(event)
        return events

    def build_all_scenarios(self, mandates: dict[str, dict] | None = None) -> dict[str, list[dict]]:
        mandates = mandates or {}
        return {
            scen_id: self.build_scenario_events(scen_id, mandate=mandates.get(scen_id))
            for scen_id in self.scenario_catalogue
        }


if __name__ == "__main__":
    pack = DataPack(Path(__file__).parent / "data")
    all_events = pack.build_all_scenarios()
    out_dir = Path(__file__).parent / "generated_events"
    out_dir.mkdir(exist_ok=True)
    for scen_id, events in all_events.items():
        with open(out_dir / f"{scen_id}.json", "w") as f:
            json.dump(events, f, indent=2)
        print(f"{scen_id}: wrote {len(events)} events -> generated_events/{scen_id}.json")
