"""Print every signal for the key real-data cases. `python scripts/demo.py`"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from conftest import auth, familiar, merchant, prior, shifted  # noqa: E402

from risk_signals import (  # noqa: E402
    DuplicateConfig,
    Item,
    PriorAuthorization,
    detect_duplicate,
    detect_lookalike_merchant,
    scan_manipulated_text,
)

BAR = "=" * 76


def show(title: str, note: str, signal) -> None:
    print(f"\n{BAR}\n{title}\n{BAR}")
    print(f"  {note}\n")
    print(f"  flag              : {signal.flag}")
    print(f"  score             : {signal.score:.3f}")
    print(f"  confidence_tier   : {signal.confidence_tier}")
    print(f"  matched_against   : {signal.matched_against_authorization_id}")
    print(f"  matched_fields    : {list(signal.matched_fields)}")
    print(f"  time_gap_minutes  : {signal.time_gap_minutes}")
    print("  reasons:")
    for r in signal.reasons:
        print(f"    - {r}")


def main() -> None:
    # 1. POSITIVE - the headline case
    show(
        "POSITIVE  AU0036 vs AU0035   [REAL DATA]",
        "Same order submitted twice, 25 min apart, related_authorization_id EMPTY on both.",
        detect_duplicate(auth("AU0036"), [prior("AU0035")]),
    )

    # 2. POSITIVE, degraded - what Viseca's own context actually gives you
    show(
        "POSITIVE (degraded)  AU0036 vs AU0035, no cart lines   [REAL DATA]",
        "Fed from context.recent_authorizations shape: 5 fields, no items, no currency.",
        detect_duplicate(auth("AU0036"), [prior("AU0035", with_items=False)]),
    )

    # 3. NEGATIVE - real rows
    show(
        "NEGATIVE  AU0013 vs AU0012   [REAL DATA]",
        "Same card, same merchant, same item_id, same day - but CHF 155 vs CHF 165 "
        "(6.1% apart). Wide 600-min window so the AMOUNT gate is what rejects it.",
        detect_duplicate(
            auth("AU0013"), [prior("AU0012")], DuplicateConfig(lookback_minutes=600)
        ),
    )

    # 4. NEGATIVE - constructed recurring-merchant pair
    base = auth("AU0035")
    sub_item = Item(
        item_id="IT0037",
        quantity=1,
        item_name="Monthly media subscription",
        item_category="subscriptions",
        unit_price=10.95,
        currency="CHF",
        item_details="One month of a digital media streaming subscription",
    )
    first = replace(
        base,
        authorization_id="SYNTH_SUB_1",
        merchant=merchant("ME0018"),
        amount=10.95,
        billing_amount_chf=10.95,
        items=(sub_item,),
        purchase_description="Monthly media subscription",
    )
    second = shifted(first, 25, "SYNTH_SUB_2")
    show(
        "NEGATIVE  recurring-capable merchant, 25 min apart   [CONSTRUCTED]",
        "CONSTRUCTED: no real close-in-time recurring pair exists (real ones are "
        "~30 days apart). Merchant ME0018 and item IT0037 are real; only the "
        "timing is synthetic. Structurally identical to case 1 EXCEPT "
        "recurring_capable=true.",
        detect_duplicate(
            second,
            [
                PriorAuthorization(
                    authorization_id=first.authorization_id,
                    timestamp=first.timestamp,
                    merchant_id=first.merchant.merchant_id,
                    billing_amount_chf=first.billing_amount_chf,
                    status="approved",
                    items=first.items,
                    currency=first.currency,
                    card_id=first.card_id,
                )
            ],
        ),
    )

    # 5. Lookalike seller
    show(
        "LOOKALIKE  AU0039 PixelHarbour vs known PixelHarbor   [REAL DATA]",
        "ME0059 'PixelHarbour' has 0 prior approved; ME0022 'PixelHarbor' has 6.",
        detect_lookalike_merchant(auth("AU0039"), familiar("CA0039")),
    )

    # 6. Injected text on a purchase that is legitimate anyway
    show(
        "INJECTED TEXT  AU0040   [REAL DATA]",
        "Text demands immediate approval. The purchase is ALSO legitimate on its "
        "facts: CHF 299 from a 6-times-used seller under a CHF 400 cap.",
        scan_manipulated_text(auth("AU0040")),
    )

    print(f"\n{BAR}\nJSON shape handed to Function 2\n{BAR}")
    print(json.dumps(detect_duplicate(auth("AU0036"), [prior("AU0035")]).to_dict(), indent=2))


if __name__ == "__main__":
    main()
