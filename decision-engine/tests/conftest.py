import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from decision_engine import Ledger  # noqa: E402
from mandate_compiler import compile_mandate  # noqa: E402


def make_event(
    *,
    hard_rules,
    uncertainty_policy="ask",
    authorization_id="AU_TEST_0001",
    card_id="CA_TEST",
    timestamp="2026-09-19T09:00:00Z",
    billing_amount_chf=10.0,
    merchant_name="Test Merchant",
    merchant_category="groceries",
    merchant_country="CH",
    items=None,
):
    """Builds a minimal authorization.request event -- enough of
    authorization_event.schema.json's shape for hard_rules.py to evaluate
    against, without depending on the full challenge data pack."""
    return {
        "type": "authorization.request",
        "authorization": {
            "authorization_id": authorization_id,
            "card_id": card_id,
            "timestamp": timestamp,
            "billing_amount_chf": billing_amount_chf,
            "merchant": {
                "merchant_name": merchant_name,
                "merchant_category": merchant_category,
                "merchant_country": merchant_country,
            },
            "items": items if items is not None else [{"item_id": "IT1", "item_name": "thing", "quantity": 1}],
        },
        "mandate": {
            "hard_rules": hard_rules,
            "uncertainty_policy": uncertainty_policy,
        },
    }


@pytest.fixture
def event_factory():
    return make_event


@pytest.fixture
def ledger():
    return Ledger()


@pytest.fixture
def worked_example_rules():
    """The Viseca worked example: CHF 20 or less, per purchase."""
    mandate = compile_mandate(
        "Buy one ordinary grocery item for CHF 20 or less from a shop I use "
        "regularly. Ask me when uncertain."
    )
    return mandate.to_viseca_dict()["hard_rules"]


@pytest.fixture
def eur_cap_rules():
    """Non-CHF currency: EUR 45 cap, still on authorization.billing_amount_chf."""
    mandate = compile_mandate("Spend up to EUR 45 on office supplies. Decline if you're not sure.")
    return mandate.to_viseca_dict()["hard_rules"]


@pytest.fixture
def weekly_lunch_rules():
    """Two rules of different scope from one sentence: a rolling purchase-count
    cap and a per-purchase amount cap."""
    mandate = compile_mandate("Order lunch no more than 3 times a week, under CHF 25 each time. Ask me if unsure.")
    return mandate.to_viseca_dict()["hard_rules"]


@pytest.fixture
def eur_fx_rates():
    """A fixture FX rate for tests -- NOT a real live rate. Convention:
    amount_in_currency * rate == amount_in_CHF, matching the challenge data
    pack's fx_rates.csv."""
    return {"EUR": 0.93}
