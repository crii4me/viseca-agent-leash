"""Load real Viseca rows from the committed fixture into typed objects."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from risk_signals import (
    Authorization,
    FamiliarMerchant,
    Item,
    Merchant,
    PriorAuthorization,
    parse_timestamp,
)

FIXTURE = Path(__file__).parent / "fixtures" / "real_authorizations.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _merchant(payload: dict) -> Merchant:
    return Merchant(
        merchant_id=payload["merchant_id"],
        merchant_name=payload["merchant_name"],
        merchant_category=payload["merchant_category"],
        merchant_country=payload["merchant_country"],
        merchant_city=payload["merchant_city"],
        availability=payload["availability"],
        recurring_capable=payload["recurring_capable"],
    )


def auth(authorization_id: str) -> Authorization:
    """Build an Authorization from a real row. Raises if the id isn't fixtured."""
    row = DATA["authorizations"].get(authorization_id)
    if row is None:
        raise KeyError(
            f"{authorization_id} is not in the fixture; add it to WANTED in "
            f"build_fixtures.py and regenerate."
        )
    return Authorization(
        authorization_id=row["authorization_id"],
        card_id=row["card_id"],
        timestamp=parse_timestamp(row["timestamp"]),
        merchant=_merchant(row["merchant"]),
        amount=row["amount"],
        currency=row["currency"],
        billing_amount_chf=row["billing_amount_chf"],
        items=tuple(
            Item(
                item_id=i["item_id"],
                quantity=i["quantity"],
                item_name=i["item_name"],
                item_category=i["item_category"],
                unit_price=i["unit_price"],
                currency=i["currency"],
                item_details=i["item_details"],
            )
            for i in row["items"]
        ),
        card_status_at_attempt=row["card_status_at_attempt"],
        authority_status=row["authority_status"],
        recent_attempt_count_10m=row["recent_attempt_count_10m"],
        customer_device_id=row["customer_device_id"],
        channel=row["channel"],
        fulfillment_method=row["fulfillment_method"],
        delivery_by=row["delivery_by"],
        order_returnable=row["order_returnable"],
        order_cancellable=row["order_cancellable"],
        purchase_description=row["purchase_description"],
        related_authorization_id=row["related_authorization_id"],
        related_authorization_status=row["related_authorization_status"],
    )


def prior(
    authorization_id: str,
    *,
    with_items: bool = True,
    status: str = "approved",
) -> PriorAuthorization:
    """Build a PriorAuthorization from a real row.

    `with_items=False` reproduces what Viseca's own
    `context.recent_authorizations` actually carries: five fields, no cart, no
    currency.
    """
    a = auth(authorization_id)
    return PriorAuthorization(
        authorization_id=a.authorization_id,
        timestamp=a.timestamp,
        merchant_id=a.merchant.merchant_id,
        billing_amount_chf=a.billing_amount_chf,
        status=status,
        items=a.items if with_items else None,
        currency=a.currency if with_items else None,
        card_id=a.card_id if with_items else None,
    )


def familiar(card_id: str) -> list[FamiliarMerchant]:
    """Merchants this card has approved transactions with, from real history."""
    return [
        FamiliarMerchant(
            merchant_id=m["merchant_id"],
            merchant_name=m["merchant_name"],
            approved_count=m["approved_count"],
        )
        for m in DATA["familiar_merchants"][card_id]
    ]


def merchant(merchant_id: str) -> Merchant:
    return _merchant(DATA["merchants"][merchant_id])


def shifted(a: Authorization, minutes: float, new_id: str) -> Authorization:
    """A copy of `a` moved forward in time - used to build constructed cases."""
    from dataclasses import replace

    return replace(a, authorization_id=new_id, timestamp=a.timestamp + timedelta(minutes=minutes))


@pytest.fixture(scope="session")
def fixture_data() -> dict:
    return DATA
