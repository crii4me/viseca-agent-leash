"""
Regenerate `real_authorizations.json` from the Viseca data pack.

Run from anywhere:

    python risk-signals/tests/fixtures/build_fixtures.py

Requires the upstream data at `vendor/viseca-2026` (see the repo README). The
generated JSON is committed, so the test suite does NOT need the vendor clone -
this script only needs to be re-run if the upstream pack changes or we want
more rows.

Everything here is real. Nothing is invented: field values are copied verbatim
from purchase_attempts.csv, purchase_attempt_items.csv and merchants.csv, and
the familiar-merchant counts are computed from approved rows in
authorization_history.csv.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA = REPO_ROOT / "vendor" / "viseca-2026" / "data"
OUT = Path(__file__).resolve().parent / "real_authorizations.json"

# The rows the tests reason about. Keep this list explicit so it is obvious
# which real authorizations the suite depends on.
WANTED = [
    "AU0012",  # SCEN0002 fully compliant shoes
    "AU0013",  # SCEN0002 same merchant/item, different price - negative case
    "AU0014",  # SCEN0002 order_returnable = false
    "AU0015",  # SCEN0002 returnable=true but text says 7 days
    "AU0016",  # SCEN0002 order_returnable = unknown
    "AU0035",  # SCEN0004 the original monitor order
    "AU0036",  # SCEN0004 the unlinked duplicate of AU0035
    "AU0037",  # SCEN0004 injected text + over cap
    "AU0039",  # SCEN0004 lookalike seller PixelHarbour
    "AU0040",  # SCEN0004 injected text, but legitimate on its facts
    "AU0042",  # SCEN0004 re-quote of the declined AU0037
    "AU0044",  # SCEN0004 unfamiliar but not lookalike
]
CARDS_FOR_HISTORY = ["CA0039", "CA0011"]


def read_csv(name: str) -> list[dict]:
    with (DATA / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def to_float(value: str) -> float | None:
    value = (value or "").strip()
    return float(value) if value else None


def to_int(value: str) -> int:
    value = (value or "").strip()
    return int(value) if value else 0


def blank_to_none(value: str) -> str | None:
    value = (value or "").strip()
    return value or None


def main() -> None:
    if not DATA.exists():
        raise SystemExit(
            f"Viseca data pack not found at {DATA}.\n"
            "Clone it first:\n"
            "  git clone --depth 1 https://github.com/START-Hack/viseca-2026.git "
            "vendor/viseca-2026"
        )

    merchants = {m["merchant_id"]: m for m in read_csv("merchants.csv")}
    attempts = {a["authorization_id"]: a for a in read_csv("purchase_attempts.csv")}

    items_by_auth: dict[str, list[dict]] = {}
    for row in read_csv("purchase_attempt_items.csv"):
        items_by_auth.setdefault(row["authorization_id"], []).append(row)

    out_auths: dict[str, dict] = {}
    for auth_id in WANTED:
        row = attempts.get(auth_id)
        if row is None:
            raise SystemExit(f"{auth_id} not found in purchase_attempts.csv")
        merchant = merchants[row["merchant_id"]]

        out_auths[auth_id] = {
            "authorization_id": row["authorization_id"],
            "scenario_id": row["scenario_id"],
            "card_id": row["card_id"],
            "timestamp": row["timestamp"],
            "merchant": {
                "merchant_id": merchant["merchant_id"],
                "merchant_name": merchant["merchant_name"],
                "merchant_category": merchant["merchant_category"],
                "merchant_country": merchant["merchant_country"],
                "merchant_city": merchant["merchant_city"],
                "availability": merchant["availability"],
                "recurring_capable": merchant["recurring_capable"] == "true",
            },
            "amount": to_float(row["amount"]),
            "currency": row["currency"],
            "billing_amount_chf": to_float(row["billing_amount_chf"]),
            "channel": row["channel"],
            "customer_device_id": blank_to_none(row["customer_device_id"]),
            "authority_status": row["authority_status"],
            "card_status_at_attempt": row["card_status_at_attempt"],
            "recent_attempt_count_10m": to_int(row["recent_attempt_count_10m"]),
            "fulfillment_method": blank_to_none(row["fulfillment_method"]),
            "delivery_by": blank_to_none(row["delivery_by"]),
            "order_returnable": row["order_returnable"],
            "order_cancellable": row["order_cancellable"],
            "related_authorization_id": blank_to_none(row["related_authorization_id"]),
            "related_authorization_status": blank_to_none(
                row["related_authorization_status"]
            ),
            "purchase_description": row["purchase_description"],
            "items": [
                {
                    "item_id": it["item_id"],
                    "item_name": it["item_name"],
                    "item_category": it["item_category"],
                    "quantity": to_int(it["quantity"]),
                    "unit_price": to_float(it["unit_price"]),
                    "currency": it["currency"],
                    "item_details": it["item_details"],
                }
                for it in sorted(
                    items_by_auth.get(auth_id, []), key=lambda r: int(r["line_no"])
                )
            ],
        }

    # Familiar merchants = approved history rows grouped by merchant, per card.
    familiar: dict[str, list[dict]] = {}
    history_path = DATA / "authorization_history.csv"
    counters: dict[str, Counter] = {c: Counter() for c in CARDS_FOR_HISTORY}
    names: dict[str, str] = {}
    with history_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            card = row["card_id"]
            if card in counters and row["status"] == "approved":
                counters[card][row["merchant_id"]] += 1
                names[row["merchant_id"]] = row["merchant_name"]
    for card, counter in counters.items():
        familiar[card] = [
            {
                "merchant_id": mid,
                "merchant_name": names.get(mid, merchants.get(mid, {}).get("merchant_name", "")),
                "approved_count": count,
            }
            for mid, count in counter.most_common()
        ]

    payload = {
        "_source": "github.com/START-Hack/viseca-2026 data pack",
        "_generated_by": "risk-signals/tests/fixtures/build_fixtures.py",
        "_note": "Every value copied verbatim from the upstream CSVs. Do not hand-edit.",
        "merchants": {
            mid: {
                "merchant_id": m["merchant_id"],
                "merchant_name": m["merchant_name"],
                "merchant_category": m["merchant_category"],
                "merchant_country": m["merchant_country"],
                "merchant_city": m["merchant_city"],
                "availability": m["availability"],
                "recurring_capable": m["recurring_capable"] == "true",
            }
            for mid, m in merchants.items()
        },
        "authorizations": out_auths,
        "familiar_merchants": familiar,
    }

    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(out_auths)} authorizations, {len(merchants)} merchants)")


if __name__ == "__main__":
    main()
